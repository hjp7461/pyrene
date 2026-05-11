"""Unit tests for the schema-RAG indexer + chunk rendering.

These cover the pure-Python pieces:
  - `render_chunk_description`: deterministic markdown blob
  - `SchemaIndexer._build_chunks` via a stubbed AsyncSession
  - `SchemaIndexer.index_all` with a mock embedder (no real OpenAI calls)

The DB-bound paths (UPSERT, ANALYZE, EXPLAIN) live in the integration suite.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from pyrene_sql.schema import (
    DEFAULT_CONNECTION_ID,
    ColumnSpec,
    SchemaChunk,
    SchemaIndexer,
    render_chunk_description,
)

# Per-test asyncio markers applied below; no module-level mark so synchronous
# tests do not get a stale event-loop fixture attached.


# --------------------------------------------------------------- chunk markdown


def test_render_chunk_description_includes_table_comment_and_columns() -> None:
    cols = (
        ColumnSpec(
            name="id", data_type="integer", is_nullable=False, description=None
        ),
        ColumnSpec(
            name="name",
            data_type="text",
            is_nullable=True,
            description="Human-readable label.",
        ),
    )
    out = render_chunk_description(
        schema="public",
        table="category",
        table_comment="Film categories.",
        columns=cols,
    )
    assert "Table: public.category" in out
    assert "Description: Film categories." in out
    assert "- id integer NOT NULL" in out
    assert "- name text NULL -- Human-readable label." in out


def test_render_chunk_description_omits_missing_comment() -> None:
    cols = (
        ColumnSpec(
            name="id", data_type="integer", is_nullable=False, description=None
        ),
    )
    out = render_chunk_description(
        schema="public", table="foo", table_comment=None, columns=cols
    )
    assert "Description:" not in out
    assert "Table: public.foo" in out


def test_render_chunk_description_is_deterministic() -> None:
    """Idempotent UPSERT relies on the rendered description being byte-stable."""
    cols = (
        ColumnSpec(
            name="id", data_type="integer", is_nullable=False, description=None
        ),
    )
    out1 = render_chunk_description(
        schema="public", table="foo", table_comment="bar", columns=cols
    )
    out2 = render_chunk_description(
        schema="public", table="foo", table_comment="bar", columns=cols
    )
    assert out1 == out2


# ------------------------------------------------------- SchemaChunk roundtrips


def test_schema_chunk_is_frozen_and_strict() -> None:
    chunk = SchemaChunk(
        connection_id=DEFAULT_CONNECTION_ID,
        schema="public",
        table="film",
        description="dummy",
        columns=(
            ColumnSpec(
                name="id",
                data_type="integer",
                is_nullable=False,
                description=None,
            ),
        ),
    )
    # frozen=True via StrictBaseModel
    with pytest.raises((TypeError, ValueError)):
        chunk.description = "mutated"


def test_schema_chunk_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        SchemaChunk(  # type: ignore[call-arg]
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table="film",
            description="dummy",
            columns=(),
            extra_field="nope",
        )


# ------------------------------------------------- SchemaIndexer.index_all mock


class _FakeResult:
    """Minimal stand-in for a SQLAlchemy `Result`."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _StubSession:
    """In-memory AsyncSession stub.

    `responses` is consulted in the order the indexer calls `execute`. For
    queries we tag (tables / columns / table_comments / column_comments) we
    pop a canned result. Any unexpected call returns an empty result.
    """

    def __init__(self, responses: dict[str, list[tuple[Any, ...]]]) -> None:
        self._responses = responses
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.committed = 0

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql_text = str(stmt).strip()
        params = params or {}
        self.executed.append((sql_text, params))

        if "information_schema.tables" in sql_text:
            return _FakeResult(self._responses.get("tables", []))
        if "information_schema.columns" in sql_text:
            return _FakeResult(self._responses.get("columns", []))
        if "pg_class" in sql_text and "pg_attribute" in sql_text:
            return _FakeResult(self._responses.get("column_comments", []))
        if "pg_class" in sql_text:
            return _FakeResult(self._responses.get("table_comments", []))
        # INSERT / DELETE / ANALYZE — return an empty result.
        return _FakeResult([])

    async def commit(self) -> None:
        self.committed += 1


class _FakeEmbedder:
    """Returns deterministic, dimension-matching vectors. Records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(i % 7) / 7.0] * 1024 for i, _ in enumerate(texts)]


async def test_index_all_round_trips_one_table() -> None:
    """End-to-end: 1 table → 1 embed call → 1 INSERT → ANALYZE → commit."""
    session = _StubSession(
        responses={
            "tables": [("public", "category")],
            "columns": [
                ("public", "category", "category_id", "integer", "NO"),
                ("public", "category", "name", "text", "YES"),
            ],
            "table_comments": [("public", "category", "Film genres.")],
            "column_comments": [
                ("public", "category", "name", "Human-readable label."),
            ],
        }
    )
    embedder = _FakeEmbedder()
    indexer = SchemaIndexer(
        write_session=session,  # type: ignore[arg-type]
        embedder=embedder,
    )

    n = await indexer.index_all()

    assert n == 1
    assert len(embedder.calls) == 1
    payload = embedder.calls[0][0]
    assert "Table: public.category" in payload
    assert "Description: Film genres." in payload
    assert "- name text NULL -- Human-readable label." in payload

    # The indexer must commit at least once (UPSERT + ANALYZE both flush).
    assert session.committed >= 1

    # There must be an INSERT and an ANALYZE in the execution log.
    sql_blobs = [sql for sql, _ in session.executed]
    assert any("INSERT INTO pyrene_schema_embeddings" in s for s in sql_blobs)
    assert any("ANALYZE pyrene_schema_embeddings" in s for s in sql_blobs)


async def test_index_all_empty_returns_zero_and_skips_embedder() -> None:
    """No tables → no embedder calls, no INSERT, return 0."""
    session = _StubSession(responses={"tables": []})
    embedder = _FakeEmbedder()
    indexer = SchemaIndexer(
        write_session=session,  # type: ignore[arg-type]
        embedder=embedder,
    )

    n = await indexer.index_all()

    assert n == 0
    assert embedder.calls == []
    sql_blobs = [sql for sql, _ in session.executed]
    assert not any("INSERT INTO" in s for s in sql_blobs)


async def test_index_all_with_reindex_issues_delete_before_insert() -> None:
    """`--reindex` path: DELETE WHERE connection_id=cid must run pre-INSERT."""
    session = _StubSession(
        responses={
            "tables": [("public", "film")],
            "columns": [
                ("public", "film", "film_id", "integer", "NO"),
            ],
        }
    )
    embedder = _FakeEmbedder()
    indexer = SchemaIndexer(
        write_session=session,  # type: ignore[arg-type]
        embedder=embedder,
    )

    await indexer.index_all(reindex=True)

    sql_blobs = [sql for sql, _ in session.executed]
    delete_idx = next(
        i
        for i, s in enumerate(sql_blobs)
        if "DELETE FROM pyrene_schema_embeddings" in s
    )
    insert_idx = next(
        i
        for i, s in enumerate(sql_blobs)
        if "INSERT INTO pyrene_schema_embeddings" in s
    )
    assert delete_idx < insert_idx


async def test_index_all_rejects_wrong_dim_embeddings() -> None:
    """Defense: if the embedder ever returns a non-1024 vector, fail loudly."""

    class _BadEmbedder:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 512 for _ in texts]

    session = _StubSession(
        responses={
            "tables": [("public", "film")],
            "columns": [("public", "film", "film_id", "integer", "NO")],
        }
    )
    indexer = SchemaIndexer(
        write_session=session,  # type: ignore[arg-type]
        embedder=_BadEmbedder(),
    )
    with pytest.raises(RuntimeError, match="vector"):
        await indexer.index_all()


async def test_index_all_uses_custom_connection_id() -> None:
    custom_cid = UUID("11111111-2222-3333-4444-555555555555")
    session = _StubSession(
        responses={
            "tables": [("public", "film")],
            "columns": [("public", "film", "film_id", "integer", "NO")],
        }
    )
    indexer = SchemaIndexer(
        write_session=session,  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),
        connection_id=custom_cid,
    )

    await indexer.index_all()

    # The INSERT must carry the custom connection_id in its bound params.
    insert_calls = [
        params
        for sql, params in session.executed
        if "INSERT INTO pyrene_schema_embeddings" in sql
    ]
    assert insert_calls, "expected at least one INSERT"
    assert insert_calls[0]["connection_id"] == custom_cid
