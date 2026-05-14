"""Schema-RAG retriever — cosine top-k against `pyrene_schema_embeddings`.

PRD-002 §4 / PLAN-002 Day 2 / PRD-042 (Hybrid chunk strategy, ADR-020).
The retriever sits between `pyrene-sql ask` and the indexer: at run time
it embeds the user's question, asks pgvector for the top-k nearest chunks
(cosine distance), and rehydrates each row into a `SchemaChunk` so the
agent's dynamic `system_prompt` can re-render them.

PRD-042 / ADR-020 — Hybrid retrieval: two separate SELECTs (one for
`chunk_type='table'`, one for `chunk_type='column'`) merged by distance
ASC. Default `k=7` splits as `k_table=2 + k_column=5` so column chunks
dominate the recall (where signal density is highest) while table chunks
preserve the aggregate context for broad queries.

Two design notes worth keeping in mind when reading the code:

1. **`columns` is intentionally not refetched.** The indexer already wrote
   the columns into `description` as a markdown blob. The retriever returns
   `SchemaChunk(columns=())` — the rendered `description` is the source of
   truth that gets injected into the prompt. Day 2 deliberately avoids
   going back to `information_schema` per query (latency + redundancy).

2. **Vector literal, not bound param.** pgvector accepts a textual
   `'[v1,v2,...]'` literal cast to `vector` — same shape used by the
   indexer. We do not pull in an SQLAlchemy `vector` dialect because the
   only call site is this one query.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_sql.schema.embeddings import EMBEDDING_DIMENSIONS, EmbeddingClient
from pyrene_sql.schema.models import (
    DEFAULT_CONNECTION_ID,
    ChunkType,
    SchemaChunk,
)

# PRD-042 / ADR-020 — default k=7 split as table 2 + column 5 (OQ-3).
_DEFAULT_K: Final[int] = 7
_TABLE_QUOTA_NUMERATOR: Final[int] = 2  # table_quota = k * 2 // 7 (rounded)
_TABLE_QUOTA_DENOMINATOR: Final[int] = 7


def _split_quota(k: int) -> tuple[int, int]:
    """Compute (k_table, k_column) so the sum equals `k` and `k_table` is
    at least 1 whenever `k >= 1`. Default `k=7` yields (2, 5).
    """
    if k <= 0:
        return (0, 0)
    if k == 1:
        return (1, 0)
    k_table = max(1, k * _TABLE_QUOTA_NUMERATOR // _TABLE_QUOTA_DENOMINATOR)
    k_column = k - k_table
    return (k_table, k_column)


class SchemaRetriever(Protocol):
    """Async retrieval interface. PRD-002 §4."""

    async def top_k(
        self,
        query: str,
        k: int = _DEFAULT_K,
        *,
        connection_id: UUID = DEFAULT_CONNECTION_ID,
    ) -> tuple[SchemaChunk, ...]:
        """Return up to `k` nearest chunks for the natural-language `query`.

        PRD-042 / ADR-020 — `k` is the *combined* upper bound across both
        chunk types. The default `k=7` splits as 2 table chunks + 5 column
        chunks (override via `_split_quota`).

        Returned tuple is ordered by ascending cosine distance (closest
        first). Empty tuple is a valid result — callers must treat it as
        "no schema context available" (PRD-002 §2.2 F2).
        """
        ...


class PgvectorRetriever:
    """Concrete `SchemaRetriever` backed by pgvector + HNSW.

    The session is expected to be bound to *any* role that can SELECT from
    `pyrene_schema_embeddings`; the read-only role created by initdb does
    have that grant, but PLAN-002 Day 1 also ran the indexer through the
    write role so either works in Phase 1.
    """

    def __init__(self, session: AsyncSession, embedder: EmbeddingClient) -> None:
        self._session = session
        self._embedder = embedder

    async def top_k(
        self,
        query: str,
        k: int = _DEFAULT_K,
        *,
        connection_id: UUID = DEFAULT_CONNECTION_ID,
    ) -> tuple[SchemaChunk, ...]:
        if k <= 0:
            return ()

        embeddings = await self._embedder.embed([query])
        if not embeddings:
            return ()
        query_vec = embeddings[0]
        if len(query_vec) != EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"embedder returned a {len(query_vec)}-dim vector, expected "
                f"{EMBEDDING_DIMENSIONS} (column is vector({EMBEDDING_DIMENSIONS}))"
            )
        vector_literal = "[" + ",".join(f"{x:.7g}" for x in query_vec) + "]"

        # PRD-021 → PRD-041: pgvector HNSW 는 approximate ANN — ef_search
        # 기본값(40)에서 후보 셋이 query 마다 다를 수 있어 cosine distance tie 가
        # 아니어도 비결정 결과 가능. PRD-021 에서 100 으로 키웠지만 2026-05-14
        # main CI 에서 86.7% (26/30) flaky 가 발현 → PRD-041 에서 200 으로 추가
        # bump (DVD Rental ~30 chunks 의 ~6.7 배 → 사실상 exact 보장).
        # production OpenAI 1024-dim 의 recall 도 추가 향상. 본 bump 는
        # 임시방편이며 근본 해법은 PRD-042 (chunk strategy rework, table →
        # column). secondary ORDER BY (schema, "table") 은 진짜 동거리 케이스의
        # alphabetical 결정성 보장.
        await self._session.execute(text("SET LOCAL hnsw.ef_search = 200"))

        # PRD-042 / ADR-020 — split into table-quota + column-quota and
        # SELECT each chunk_type separately so HNSW filters by chunk_type
        # before the cosine ranking. Per-quota result is already distance
        # ASC; we then merge by distance for the final ordering.
        k_table, k_column = _split_quota(k)
        table_rows = await self._select_by_chunk_type(
            chunk_type="table",
            vector_literal=vector_literal,
            connection_id=connection_id,
            limit=k_table,
        )
        column_rows = await self._select_by_chunk_type(
            chunk_type="column",
            vector_literal=vector_literal,
            connection_id=connection_id,
            limit=k_column,
        )

        merged = _merge_by_distance(table_rows, column_rows)[:k]

        # `columns` is empty by design — see module docstring. The rendered
        # `description` already carries the column list verbatim, and the
        # retriever's only consumer (the dynamic system_prompt) renders the
        # description, not the structured columns tuple.
        return tuple(
            SchemaChunk(
                connection_id=connection_id,
                schema=row[0],
                table=row[1],
                description=row[4],
                columns=(),
                chunk_type=row[2],
                column_name=row[3],
            )
            for row in merged
        )

    async def _select_by_chunk_type(
        self,
        *,
        chunk_type: ChunkType,
        vector_literal: str,
        connection_id: UUID,
        limit: int,
    ) -> list[tuple[Any, ...]]:
        """Issue one chunk_type-filtered SELECT and return raw rows.

        Rows are
            (schema, "table", chunk_type, column_name, description, distance)
        ordered by distance ASC, then schema ASC, then "table" ASC,
        then column_name ASC (PRD-021 secondary ORDER BY invariant +
        column_name tertiary for column chunks).
        """
        if limit <= 0:
            return []
        stmt = text(
            """
            SELECT schema,
                   "table",
                   chunk_type,
                   column_name,
                   description,
                   embedding <=> CAST(:qv AS vector) AS distance
              FROM pyrene_schema_embeddings
             WHERE connection_id = :cid
               AND chunk_type = :ct
             ORDER BY embedding <=> CAST(:qv AS vector),
                      schema ASC,
                      "table" ASC,
                      column_name ASC
             LIMIT :k
            """
        )
        result = await self._session.execute(
            stmt,
            {
                "cid": connection_id,
                "qv": vector_literal,
                "ct": chunk_type,
                "k": limit,
            },
        )
        # SQLAlchemy Row → plain tuple so the merge helper stays Row-agnostic
        # (and unit-testable without a real engine).
        return [tuple(row) for row in result.fetchall()]


def _merge_by_distance(
    table_rows: Sequence[tuple[Any, ...]],
    column_rows: Sequence[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    """Stable merge of two distance-ASC row lists by their `distance` column
    (index 5). Pure helper so unit tests can exercise the merge without a DB.
    """
    return sorted(
        list(table_rows) + list(column_rows),
        key=lambda r: (r[5], r[0], r[1], r[3]),
    )


__all__ = [
    "PgvectorRetriever",
    "SchemaRetriever",
    "_merge_by_distance",
    "_split_quota",
]
