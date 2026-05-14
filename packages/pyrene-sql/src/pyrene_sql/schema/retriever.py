"""Schema-RAG retriever — cosine top-k against `pyrene_schema_embeddings`.

PRD-002 §4 / PLAN-002 Day 2. The retriever sits between `pyrene-sql ask` and
the indexer: at run time it embeds the user's question, asks pgvector for the
top-k nearest table chunks (cosine distance), and rehydrates each row into a
`SchemaChunk` so the agent's dynamic `system_prompt` can re-render them.

Two design notes worth keeping in mind when reading the code:

1. **`columns` is intentionally not refetched.** The indexer already wrote the
   columns into `description` as a markdown blob (see
   `pyrene_sql.schema.indexer.render_chunk_description`). The retriever returns
   `SchemaChunk(columns=())` — the rendered `description` is the source of
   truth that gets injected into the prompt. Day 2 deliberately avoids going
   back to `information_schema` per query (latency + redundancy).

2. **Vector literal, not bound param.** pgvector accepts a textual
   `'[v1,v2,...]'` literal cast to `vector` — same shape used by the indexer.
   We do not pull in an SQLAlchemy `vector` dialect because the only call site
   is this one query.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_sql.schema.embeddings import EMBEDDING_DIMENSIONS, EmbeddingClient
from pyrene_sql.schema.models import DEFAULT_CONNECTION_ID, SchemaChunk


class SchemaRetriever(Protocol):
    """Async retrieval interface. PRD-002 §4."""

    async def top_k(
        self,
        query: str,
        k: int = 3,
        *,
        connection_id: UUID = DEFAULT_CONNECTION_ID,
    ) -> tuple[SchemaChunk, ...]:
        """Return up to `k` nearest table chunks for the natural-language `query`.

        Returned tuple is ordered by ascending cosine distance (closest first).
        Empty tuple is a valid result — callers must treat it as "no schema
        context available" (PRD-002 §2.2 F2).
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
        k: int = 3,
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

        stmt = text(
            """
            SELECT schema, "table", description
              FROM pyrene_schema_embeddings
             WHERE connection_id = :cid
             ORDER BY embedding <=> CAST(:qv AS vector),
                      schema ASC,
                      "table" ASC
             LIMIT :k
            """
        )
        result = await self._session.execute(
            stmt,
            {"cid": connection_id, "qv": vector_literal, "k": k},
        )
        rows = result.fetchall()

        # `columns` is empty by design — see module docstring. The rendered
        # `description` already carries the column list verbatim, and the
        # retriever's only consumer (the dynamic system_prompt) renders the
        # description, not the structured columns tuple.
        return tuple(
            SchemaChunk(
                connection_id=connection_id,
                schema=row[0],
                table=row[1],
                description=row[2],
                columns=(),
            )
            for row in rows
        )


__all__ = ["PgvectorRetriever", "SchemaRetriever"]
