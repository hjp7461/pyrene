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

    `responses` is the list of rows the next `.execute()` call returns.
    """

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append((str(stmt), params or {}))
        return _FakeResult(self.rows)


# ---------------------------------------------------------- PgvectorRetriever


async def test_top_k_emits_cosine_distance_order_by() -> None:
    """The query must use the `<=>` operator (cosine distance) and a LIMIT."""
    session = _FakeSession(
        rows=[
            ("public", "payment", "Table: public.payment\n..."),
            ("public", "rental", "Table: public.rental\n..."),
            ("public", "film", "Table: public.film\n..."),
        ]
    )
    embedder = _FakeEmbedder()
    retriever = PgvectorRetriever(session=session, embedder=embedder)  # type: ignore[arg-type]

    chunks = await retriever.top_k("monthly revenue", k=3)

    # The retriever embedded the query exactly once with the user text.
    assert embedder.calls == [["monthly revenue"]]

    # PRD-021 → PRD-041: retriever emits `SET LOCAL hnsw.ef_search = 200` to
    # tame HNSW approximate ANN before the SELECT. Filter to the SELECT call
    # for the cosine/order/limit checks.
    select_calls = [
        (sql, params) for sql, params in session.executed if "<=>" in sql
    ]
    assert len(select_calls) == 1
    sql, params = select_calls[0]
    assert "<=>" in sql
    assert "ORDER BY embedding" in sql
    assert "LIMIT :k" in sql
    assert params["cid"] == DEFAULT_CONNECTION_ID
    assert params["k"] == 3
    # Vector literal must be a `[v1,v2,...]` string (pgvector textual form).
    assert isinstance(params["qv"], str)
    assert params["qv"].startswith("[") and params["qv"].endswith("]")
    # 1024 dims separated by commas (1023 commas).
    assert params["qv"].count(",") == 1023

    # Rows round-trip into SchemaChunks in the DB-returned order.
    assert tuple(c.table for c in chunks) == ("payment", "rental", "film")
    assert all(isinstance(c, SchemaChunk) for c in chunks)
    assert chunks[0].description.startswith("Table: public.payment")


async def test_top_k_respects_custom_connection_id() -> None:
    """Phase 2 multi-tenant path: `connection_id` is a bound param, not hard-coded."""
    from uuid import UUID

    other_cid = UUID("11111111-2222-3333-4444-555555555555")
    session = _FakeSession(rows=[])
    retriever = PgvectorRetriever(
        session=session,  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),
    )

    await retriever.top_k("query", k=3, connection_id=other_cid)

    # PRD-021: SET LOCAL ef_search 호출이 앞서므로 SELECT 만 필터링.
    select_calls = [
        (sql, params) for sql, params in session.executed if "<=>" in sql
    ]
    _, params = select_calls[0]
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
