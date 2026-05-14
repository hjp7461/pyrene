"""SchemaIndexer: read information_schema → embed → UPSERT into pgvector.

PRD-002 §2.1 S1, L-03 (table-level chunks to start). Flow:

1. Query `information_schema.tables` for BASE TABLEs in `public` (and any
   other schema the operator chooses later — Phase 1 is single-schema).
2. Query `information_schema.columns` for column metadata.
3. Pull `pg_description` to recover the `COMMENT ON TABLE / COLUMN` bodies
   the initdb script (`04-table-comments.sql`) wrote.
4. Render one markdown chunk per table that embeds and re-renders cleanly.
5. Embed each chunk's description in a single batched API call.
6. UPSERT into `pyrene_schema_embeddings`, keyed on
   `(connection_id, schema, "table")`.
7. `ANALYZE pyrene_schema_embeddings;` so the HNSW planner stats reflect the
   loaded rows (ADR-013 (c) — first cosine query must not seq-scan).

We deliberately exclude `pg_*` and `information_schema` schemas (no point
embedding catalog metadata) and exclude any schema starting with `pg_` to
guard against future temporary-schema leakage from CI containers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import logfire
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core import SPAN_SCHEMA_INDEX
from pyrene_sql.schema.embeddings import EMBEDDING_DIMENSIONS, EmbeddingClient
from pyrene_sql.schema.models import DEFAULT_CONNECTION_ID, ColumnSpec, SchemaChunk

# Schemas the indexer never embeds. `information_schema` and any `pg_*`
# schema are catalog plumbing — they have no business value for NL queries
# and would dominate the index with thousands of useless rows.
_EXCLUDED_SCHEMAS: Final[frozenset[str]] = frozenset(
    {"information_schema", "pg_catalog", "pg_toast"}
)


@dataclass(frozen=True, slots=True)
class _TableKey:
    """Internal helper — (schema, table) tuple with stable hashing."""

    schema: str
    table: str


def render_chunk_description(
    *,
    schema: str,
    table: str,
    table_comment: str | None,
    columns: tuple[ColumnSpec, ...],
) -> str:
    """Render a deterministic markdown blob for one table.

    The blob is what gets embedded *and* what the Day 2 retriever re-injects
    into the system prompt, so it must be (a) human-readable, (b) compact
    enough to keep top-3 under 2000 tokens (PRD-002 §6), and (c) stable
    across runs (idempotent UPSERT depends on equality of the text).
    """
    lines: list[str] = [f"Table: {schema}.{table}"]
    if table_comment and table_comment.strip():
        lines.append(f"Description: {table_comment.strip()}")
    lines.append("Columns:")
    for col in columns:
        nullable = "NULL" if col.is_nullable else "NOT NULL"
        suffix = f" -- {col.description}" if col.description else ""
        lines.append(f"  - {col.name} {col.data_type} {nullable}{suffix}")
    return "\n".join(lines)


def render_column_chunk_description(
    *, schema: str, table: str, column: ColumnSpec
) -> str:
    """PRD-042 / ADR-020 — Hybrid chunk strategy column-level description.

    OQ-2 중간 포맷:
        'Column: {schema}.{table}.{name} ({data_type}, {NULL|NOT NULL}) -- {comment}'

    Compact enough to keep ≤ 30 tokens per column on average; the optional
    column comment is the recall-driving suffix (e.g. "rental fee per day").
    """
    nullable = "NULL" if column.is_nullable else "NOT NULL"
    suffix = f" -- {column.description}" if column.description else ""
    return (
        f"Column: {schema}.{table}.{column.name} "
        f"({column.data_type}, {nullable}){suffix}"
    )


class SchemaIndexer:
    """Drives information_schema → embed → UPSERT into `pyrene_schema_embeddings`.

    The write session is required because we both read information_schema
    (the readonly role can also read it, but mixing roles inside a single
    indexer is asking for surprises in PLAN-011) and write to the embeddings
    table (readonly role cannot).
    """

    def __init__(
        self,
        *,
        write_session: AsyncSession,
        embedder: EmbeddingClient,
        connection_id: UUID = DEFAULT_CONNECTION_ID,
        include_schemas: tuple[str, ...] = ("public",),
    ) -> None:
        self._session = write_session
        self._embedder = embedder
        self._connection_id = connection_id
        self._include_schemas = include_schemas

    async def index_all(self, *, reindex: bool = False) -> int:
        """Index every (schema, table) pair under `include_schemas`.

        Returns:
            The number of chunks UPSERTed.

        Args:
            reindex: If True, delete every row for this connection_id first
                so removed tables disappear from the index. Idempotent UPSERT
                covers the common case (rerun after seeding); `reindex=True`
                is the escape hatch for when a table was dropped upstream.
        """
        with logfire.span(
            SPAN_SCHEMA_INDEX,
            connection_id=str(self._connection_id),
            include_schemas=list(self._include_schemas),
            reindex=reindex,
        ) as span:
            chunks = await self._build_chunks()
            if not chunks:
                span.set_attribute("chunk_count", 0)
                return 0

            descriptions = [chunk.description for chunk in chunks]
            embeddings = await self._embedder.embed(descriptions)

            if len(embeddings) != len(chunks):
                raise RuntimeError(
                    f"embedder returned {len(embeddings)} vectors for "
                    f"{len(chunks)} chunks"
                )
            for vec in embeddings:
                if len(vec) != EMBEDDING_DIMENSIONS:
                    raise RuntimeError(
                        f"embedding has {len(vec)} dims, expected "
                        f"{EMBEDDING_DIMENSIONS} (column is vector({EMBEDDING_DIMENSIONS}))"
                    )

            if reindex:
                await self._session.execute(
                    text(
                        "DELETE FROM pyrene_schema_embeddings "
                        "WHERE connection_id = :cid"
                    ),
                    {"cid": self._connection_id},
                )

            await self._upsert(chunks, embeddings)
            await self._session.commit()

            # ANALYZE outside the transaction so planner stats are visible to
            # the next cosine query. Wrapping ANALYZE in a transaction works,
            # but committing first avoids "ANALYZE cannot run inside a
            # transaction block" surprises with some drivers / pool modes.
            await self._session.execute(text("ANALYZE pyrene_schema_embeddings"))
            await self._session.commit()
            span.set_attribute("chunk_count", len(chunks))
            return len(chunks)

    # ------------------------------------------------------------------ chunks

    async def _build_chunks(self) -> list[SchemaChunk]:
        """Pull table list + columns + COMMENT ON bodies → list[SchemaChunk].

        PRD-042 / ADR-020: emits *both* table chunks (1 per table) and
        column chunks (N per table) so the retriever can do two-stage
        Hybrid retrieval. Per-table emission order is `table, then columns`
        so list[i] of `description` aligns 1:1 with list[i] of embedding
        in the batched embed call downstream.
        """
        tables = await self._fetch_tables()
        columns_by_table = await self._fetch_columns(tables)
        comments = await self._fetch_comments(tables)

        chunks: list[SchemaChunk] = []
        for key in tables:
            cols = columns_by_table.get(key, ())
            table_comment = comments.tables.get(key)
            cols_with_comments = tuple(
                ColumnSpec(
                    name=col.name,
                    data_type=col.data_type,
                    is_nullable=col.is_nullable,
                    description=comments.columns.get((key, col.name)),
                )
                for col in cols
            )
            # 1) Table chunk (PRD-002 §4 backward compat).
            table_description = render_chunk_description(
                schema=key.schema,
                table=key.table,
                table_comment=table_comment,
                columns=cols_with_comments,
            )
            chunks.append(
                SchemaChunk(
                    connection_id=self._connection_id,
                    schema=key.schema,
                    table=key.table,
                    description=table_description,
                    columns=cols_with_comments,
                    chunk_type="table",
                    column_name="",
                )
            )
            # 2) Column chunks (PRD-042 / ADR-020 — Hybrid emit).
            for col in cols_with_comments:
                col_description = render_column_chunk_description(
                    schema=key.schema, table=key.table, column=col
                )
                chunks.append(
                    SchemaChunk(
                        connection_id=self._connection_id,
                        schema=key.schema,
                        table=key.table,
                        description=col_description,
                        columns=(col,),
                        chunk_type="column",
                        column_name=col.name,
                    )
                )
        return chunks

    async def _fetch_tables(self) -> list[_TableKey]:
        stmt = text(
            """
            SELECT table_schema, table_name
              FROM information_schema.tables
             WHERE table_type = 'BASE TABLE'
               AND table_schema = ANY(:schemas)
               AND table_schema NOT IN :excluded
             ORDER BY table_schema, table_name
            """
        ).bindparams(bindparam("excluded", expanding=True))
        result = await self._session.execute(
            stmt,
            {
                "schemas": list(self._include_schemas),
                "excluded": list(_EXCLUDED_SCHEMAS),
            },
        )
        return [_TableKey(schema=row[0], table=row[1]) for row in result.fetchall()]

    async def _fetch_columns(
        self, tables: list[_TableKey]
    ) -> dict[_TableKey, tuple[ColumnSpec, ...]]:
        if not tables:
            return {}

        # Build per-row pair list so we can filter on (schema, table) tuples.
        schemas = list({t.schema for t in tables})
        names = list({t.table for t in tables})
        stmt = text(
            """
            SELECT table_schema, table_name, column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_schema = ANY(:schemas)
               AND table_name   = ANY(:names)
             ORDER BY table_schema, table_name, ordinal_position
            """
        )
        result = await self._session.execute(
            stmt, {"schemas": schemas, "names": names}
        )

        wanted: set[tuple[str, str]] = {(t.schema, t.table) for t in tables}
        bucket: dict[_TableKey, list[ColumnSpec]] = {t: [] for t in tables}
        for schema, name, col, dtype, is_nullable in result.fetchall():
            if (schema, name) not in wanted:
                continue
            key = _TableKey(schema=schema, table=name)
            bucket[key].append(
                ColumnSpec(
                    name=col,
                    data_type=dtype,
                    is_nullable=(is_nullable == "YES"),
                    description=None,
                )
            )
        return {k: tuple(v) for k, v in bucket.items()}

    async def _fetch_comments(self, tables: list[_TableKey]) -> _Comments:
        """Read `COMMENT ON TABLE / COLUMN` via pg_description.

        information_schema does not expose comments directly, so we join
        pg_class + pg_namespace + pg_description for tables and
        pg_attribute for columns.
        """
        if not tables:
            return _Comments(tables={}, columns={})

        schemas = list({t.schema for t in tables})
        names = list({t.table for t in tables})

        table_stmt = text(
            """
            SELECT n.nspname, c.relname, d.description
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              LEFT JOIN pg_description d
                     ON d.objoid = c.oid
                    AND d.objsubid = 0
             WHERE c.relkind = 'r'
               AND n.nspname = ANY(:schemas)
               AND c.relname = ANY(:names)
            """
        )
        table_rows = (
            await self._session.execute(
                table_stmt, {"schemas": schemas, "names": names}
            )
        ).fetchall()
        wanted_keys: set[_TableKey] = set(tables)
        table_comments: dict[_TableKey, str] = {}
        for schema, name, desc in table_rows:
            key = _TableKey(schema=schema, table=name)
            if key in wanted_keys and desc is not None:
                table_comments[key] = desc

        column_stmt = text(
            """
            SELECT n.nspname, c.relname, a.attname, d.description
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_attribute a
                ON a.attrelid = c.oid
               AND a.attnum > 0
               AND NOT a.attisdropped
              JOIN pg_description d
                ON d.objoid = c.oid
               AND d.objsubid = a.attnum
             WHERE c.relkind = 'r'
               AND n.nspname = ANY(:schemas)
               AND c.relname = ANY(:names)
            """
        )
        column_rows = (
            await self._session.execute(
                column_stmt, {"schemas": schemas, "names": names}
            )
        ).fetchall()
        column_comments: dict[tuple[_TableKey, str], str] = {}
        for schema, name, attname, desc in column_rows:
            key = _TableKey(schema=schema, table=name)
            if key in wanted_keys and desc is not None:
                column_comments[(key, attname)] = desc

        return _Comments(tables=table_comments, columns=column_comments)

    # ------------------------------------------------------------------ upsert

    async def _upsert(
        self,
        chunks: list[SchemaChunk],
        embeddings: list[list[float]],
    ) -> None:
        """Idempotent INSERT … ON CONFLICT keyed on the 5-tuple
        (connection_id, schema, "table", chunk_type, column_name) — PRD-042
        / ADR-020 Hybrid chunk strategy. Table chunks use column_name=''
        as the sentinel; column chunks use the actual column name.
        """
        sql = text(
            """
            INSERT INTO pyrene_schema_embeddings (
                connection_id, schema, "table",
                chunk_type, column_name,
                description, embedding, updated_at
            )
            VALUES (
                :connection_id, :schema, :table,
                :chunk_type, :column_name,
                :description, CAST(:embedding AS vector), NOW()
            )
            ON CONFLICT (connection_id, schema, "table", chunk_type, column_name)
            DO UPDATE
              SET description = EXCLUDED.description,
                  embedding   = EXCLUDED.embedding,
                  updated_at  = NOW()
            """
        )
        for chunk, vec in zip(chunks, embeddings, strict=True):
            # pgvector accepts a textual `'[v1,v2,...]'` literal cast to
            # vector — works without an SQLAlchemy vector dialect.
            vector_literal = "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
            await self._session.execute(
                sql,
                {
                    "connection_id": chunk.connection_id,
                    "schema": chunk.schema,
                    "table": chunk.table,
                    "chunk_type": chunk.chunk_type,
                    "column_name": chunk.column_name,
                    "description": chunk.description,
                    "embedding": vector_literal,
                },
            )


@dataclass(frozen=True, slots=True)
class _Comments:
    """Internal carrier for `pg_description` fetches."""

    tables: dict[_TableKey, str]
    columns: dict[tuple[_TableKey, str], str]


__all__ = ["SchemaIndexer", "render_chunk_description"]
