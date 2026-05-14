"""Unit tests for the schema-RAG retriever + dynamic system prompt.

These tests do not require Docker. They use stub embedders + stub sessions
to exercise:

  - `PgvectorRetriever.top_k` issues a `<=> CAST(:qv AS vector)` query in the
    correct shape and returns SchemaChunks ordered by the DB row order
    (which the DB itself orders by cosine distance).
  - `agent._schema_context` is a no-op when `Deps.schema_retriever is None`
    (PLAN-002 §83 — Phase 1 nullability is a documented contract).
  - The token cap actually trims oversized retriever output.

DB-side ordering / HNSW behavior is covered by the integration tests
(`tests/integration/test_retriever_db.py`).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from pyrene_sql.agent import (
    SCHEMA_PROMPT_TOKEN_CAP,
    _schema_context,
    _trim_to_token_cap,
)
from pyrene_sql.deps import Deps
from pyrene_sql.schema import DEFAULT_CONNECTION_ID, PgvectorRetriever, SchemaChunk

# Per-test asyncio markers applied below where needed; no module-level mark
# so the two synchronous `_trim_to_token_cap` tests do not get an event-loop
# fixture attached.


# ----------------------------------------------------- helpers + tiny fakes


class _FakeEmbedder:
    """Deterministic 1024-dim embedder. Records every embed call."""

    def __init__(self, *, vector: list[float] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._vector = vector if vector is not None else [0.1] * 1024

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [list(self._vector) for _ in texts]


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeSession:
    """Minimal AsyncSession stub: records every execute() call.

    PRD-042: rows can be supplied per-chunk_type (table/column) via
    `rows_by_chunk_type`, falling back to a single `rows` list. SET LOCAL
    statements (no `ct` param) always receive the fallback (typically
    empty).
    """

    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        *,
        rows_by_chunk_type: dict[str, list[tuple[Any, ...]]] | None = None,
    ) -> None:
        self.rows = rows or []
        self._rows_by_chunk_type = rows_by_chunk_type or {}
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        self.executed.append((str(stmt), params))
        ct = params.get("ct")
        if isinstance(ct, str) and ct in self._rows_by_chunk_type:
            return _FakeResult(self._rows_by_chunk_type[ct])
        return _FakeResult(self.rows)


# ---------------------------------------------------------- PgvectorRetriever


async def test_top_k_emits_cosine_distance_order_by() -> None:
    """PRD-042 Hybrid retrieval — two chunk_type-filtered SELECTs (table +
    column), each using `<=>`, with merged distance-ASC ordering at the end.
    """
    # row format (PRD-042): (schema, table, chunk_type, column_name, description, distance)
    payment_amount_desc = "Column: public.payment.amount ..."
    film_rating_desc = "Column: public.film.rating ..."
    session = _FakeSession(
        rows_by_chunk_type={
            "table": [
                ("public", "payment", "table", "", "Table: public.payment\n...", 0.10),
                ("public", "rental", "table", "", "Table: public.rental\n...", 0.30),
            ],
            "column": [
                ("public", "payment", "column", "amount", payment_amount_desc, 0.05),
                ("public", "film", "column", "rating", film_rating_desc, 0.20),
            ],
        }
    )
    embedder = _FakeEmbedder()
    retriever = PgvectorRetriever(session=session, embedder=embedder)  # type: ignore[arg-type]

    chunks = await retriever.top_k("monthly revenue", k=7)

    # The retriever embedded the query exactly once with the user text.
    assert embedder.calls == [["monthly revenue"]]

    # PRD-021 → PRD-041: SET LOCAL hnsw.ef_search precedes SELECT. Filter to
    # the SELECT calls (those use the `<=>` operator).
    select_calls = [
        (sql, params) for sql, params in session.executed if "<=>" in sql
    ]
    # PRD-042: exactly two chunk_type-filtered SELECTs (table + column).
    assert len(select_calls) == 2
    chunk_types_called = sorted(params["ct"] for _sql, params in select_calls)
    assert chunk_types_called == ["column", "table"]

    # Each SELECT shares the same vector + cid + ORDER BY skeleton.
    for sql, params in select_calls:
        assert "<=>" in sql
        assert "ORDER BY embedding" in sql
        assert "LIMIT :k" in sql
        assert "chunk_type = :ct" in sql
        assert params["cid"] == DEFAULT_CONNECTION_ID
        assert isinstance(params["qv"], str)
        assert params["qv"].startswith("[") and params["qv"].endswith("]")
        assert params["qv"].count(",") == 1023

    # Final result is distance-ASC merged across both chunk types and capped to k.
    distances_in_order = [0.05, 0.10, 0.20, 0.30]
    assert tuple(c.table for c in chunks) == ("payment", "payment", "film", "rental")
    # And the chunk_type field round-trips correctly so the prompt renderer
    # can distinguish table vs column chunks downstream.
    assert tuple(c.chunk_type for c in chunks) == (
        "column", "table", "column", "table"
    )
    # All entries are SchemaChunks regardless of chunk_type.
    assert all(isinstance(c, SchemaChunk) for c in chunks)
    # Distance ordering is what the merge helper guarantees — exercised
    # implicitly here, exhaustively in test_split_quota_and_merge.py below.
    assert len(distances_in_order) == len(chunks)


async def test_top_k_respects_custom_connection_id() -> None:
    """Phase 2 multi-tenant path: `connection_id` is a bound param, not hard-coded.
    PRD-042: both chunk_type SELECTs receive the same cid.
    """
    from uuid import UUID

    other_cid = UUID("11111111-2222-3333-4444-555555555555")
    session = _FakeSession(rows=[])
    retriever = PgvectorRetriever(
        session=session,  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),
    )

    await retriever.top_k("query", k=7, connection_id=other_cid)

    # PRD-042: 2 SELECTs (table + column), both with the custom cid.
    select_calls = [
        (sql, params) for sql, params in session.executed if "<=>" in sql
    ]
    assert len(select_calls) == 2
    for _sql, params in select_calls:
        assert params["cid"] == other_cid


async def test_top_k_returns_empty_tuple_for_non_positive_k() -> None:
    """`k <= 0` is a guard, not an error — return ()."""
    session = _FakeSession(rows=[])
    embedder = _FakeEmbedder()
    retriever = PgvectorRetriever(session=session, embedder=embedder)  # type: ignore[arg-type]

    assert await retriever.top_k("q", k=0) == ()
    # And we did NOT issue an embed or a DB hit for the no-op path.
    assert embedder.calls == []
    assert session.executed == []


async def test_top_k_validates_embedding_dimensions() -> None:
    """A wrong-dim embedder must fail loudly — silent mis-shape would corrupt query."""

    class _BadDimEmbedder:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 512 for _ in texts]

    session = _FakeSession(rows=[])
    retriever = PgvectorRetriever(
        session=session,  # type: ignore[arg-type]
        embedder=_BadDimEmbedder(),
    )

    with pytest.raises(RuntimeError, match="1024"):
        await retriever.top_k("q", k=3)


# -------------------------------------------------- agent._schema_context


async def test_schema_context_returns_empty_when_retriever_is_none() -> None:
    """No retriever → no schema section. The static prompt is the whole instruction."""
    deps = Deps(db=AsyncMock(), schema_retriever=None)
    # Pass a minimal RunContext-shaped object; only `deps` and `prompt` are read.
    ctx = _make_ctx(deps=deps, prompt="some question")

    out = await _schema_context(ctx)
    assert out == ""


async def test_schema_context_returns_empty_when_prompt_is_empty() -> None:
    """Multi-modal / empty prompt → no embedding, no section."""

    class _NeverCalledRetriever:
        async def top_k(
            self, query: str, k: int = 3, **_: Any
        ) -> tuple[SchemaChunk, ...]:
            raise AssertionError("retriever must not be called with empty prompt")

    deps = Deps(db=AsyncMock(), schema_retriever=_NeverCalledRetriever())
    ctx = _make_ctx(deps=deps, prompt="")

    out = await _schema_context(ctx)
    assert out == ""


async def test_schema_context_renders_retriever_output() -> None:
    """Happy path: retriever returns 2 chunks → output contains both descriptions."""

    class _StubRetriever:
        async def top_k(
            self, query: str, k: int = 3, **_: Any
        ) -> tuple[SchemaChunk, ...]:
            return (
                SchemaChunk(
                    connection_id=DEFAULT_CONNECTION_ID,
                    schema="public",
                    table="payment",
                    description="Table: public.payment\nDescription: payments.",
                    columns=(),
                ),
                SchemaChunk(
                    connection_id=DEFAULT_CONNECTION_ID,
                    schema="public",
                    table="rental",
                    description="Table: public.rental\nDescription: rentals.",
                    columns=(),
                ),
            )

    deps = Deps(db=AsyncMock(), schema_retriever=_StubRetriever())
    ctx = _make_ctx(deps=deps, prompt="monthly revenue")
    out = await _schema_context(ctx)

    assert "Relevant tables" in out
    assert "Table: public.payment" in out
    assert "Table: public.rental" in out


async def test_schema_context_returns_empty_when_retriever_returns_no_rows() -> None:
    """Empty index (PRD-002 §2.2 F2) → empty section, no exception."""

    class _EmptyRetriever:
        async def top_k(
            self, query: str, k: int = 3, **_: Any
        ) -> tuple[SchemaChunk, ...]:
            return ()

    deps = Deps(db=AsyncMock(), schema_retriever=_EmptyRetriever())
    ctx = _make_ctx(deps=deps, prompt="anything")
    out = await _schema_context(ctx)
    assert out == ""


# -------------------------------------------------------- token-budget guard


def test_trim_to_token_cap_passes_short_input_unchanged() -> None:
    short = "Table: public.foo\nDescription: small."
    out = _trim_to_token_cap(short, SCHEMA_PROMPT_TOKEN_CAP)
    assert out == short


def test_trim_to_token_cap_trims_oversized_input() -> None:
    """Oversized blob must shrink AND retain the truncation marker."""
    # ~12000 chars → ~3000 tokens by tiktoken, well above the 2000 cap.
    oversized = ("Table: public.foo\nDescription: " + "x" * 50 + "\n") * 200
    out = _trim_to_token_cap(oversized, SCHEMA_PROMPT_TOKEN_CAP)

    assert out != oversized
    assert "schema context truncated" in out
    # Re-estimate must land at or below the cap (with a small ceiling for
    # tokenizer slack — the trim function targets 95% of the cap).
    from pyrene_sql.agent import _estimate_tokens

    assert _estimate_tokens(out) <= SCHEMA_PROMPT_TOKEN_CAP + 50


async def test_schema_context_caps_total_tokens() -> None:
    """End-to-end: a retriever returning enormous chunks still respects the cap."""

    class _HugeRetriever:
        async def top_k(
            self, query: str, k: int = 3, **_: Any
        ) -> tuple[SchemaChunk, ...]:
            blob = "Table: public.huge\nDescription: " + ("xyz " * 2000)
            return tuple(
                SchemaChunk(
                    connection_id=DEFAULT_CONNECTION_ID,
                    schema="public",
                    table=f"huge_{i}",
                    description=blob,
                    columns=(),
                )
                for i in range(3)
            )

    deps = Deps(db=AsyncMock(), schema_retriever=_HugeRetriever())
    ctx = _make_ctx(deps=deps, prompt="big query")
    out = await _schema_context(ctx)

    from pyrene_sql.agent import _estimate_tokens

    assert _estimate_tokens(out) <= SCHEMA_PROMPT_TOKEN_CAP + 50
    assert "schema context truncated" in out


# --------------------------------------------------------------- ctx helper


def _make_ctx(*, deps: Deps, prompt: str) -> Any:
    """Build a minimal object with the two RunContext attrs we actually read.

    Pydantic AI's `RunContext` has ~15 required fields; constructing a real
    one in tests pulls in `models.Model`, `RunUsage`, etc. The dynamic
    system_prompt function only reads `ctx.deps` and `ctx.prompt`, so a
    duck-typed `SimpleNamespace` is sufficient here.
    """
    from types import SimpleNamespace

    return SimpleNamespace(deps=deps, prompt=prompt)
