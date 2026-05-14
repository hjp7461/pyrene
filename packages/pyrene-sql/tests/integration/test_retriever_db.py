"""Integration tests for PgvectorRetriever + the 30-query retrieval dataset.

PRD-045: deterministic replay of *production* OpenAI
`text-embedding-3-small @ 1024-dim` vectors, loaded from
`tests/data/embedding_cache.json`. CI runs offline (no `OPENAI_API_KEY`
needed); fixture regeneration is the explicit step
`bin/regenerate_embedding_cache.py`.

These tests are the regression guard for L-03 (PRD-002 §7): if top-3
accuracy drops below 95% on the canned dataset
(`tests/data/schema_retrieval_30.yaml`), retrieval has regressed and we
need to revisit chunk strategy / ef_search / scoring. We fail the suite
rather than skip — silent regression on retrieval is exactly the failure
mode PLAN-002 calls out.

Why cache replay, not real OpenAI on every CI run:
  - The integration *contract* under test is "embed → cosine query →
    top-k rehydration → SchemaChunk". Live OpenAI gives us a different
    metric (provider drift, network) on top of pipeline correctness.
  - For 30 fixed queries + 218 fixed chunk descriptions, we want byte-
    stable vectors so the same DB + same code reproduces the same
    accuracy across runs. PRD-044 measured production at 100% top-3
    across 3 variants; the cache replays that fact deterministically.

Why cache miss = strict raise:
  - Silent fallback (live fetch, zero vector) would mask dataset/seed
    drift. Strict raise with the regen command in the error message
    surfaces the cause to whoever changed the fixture.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from pyrene_sql.schema import (
    PgvectorRetriever,
    SchemaIndexer,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_FILE = _DATA_DIR / "schema_retrieval_30.yaml"
CACHE_FILE = _DATA_DIR / "embedding_cache.json"


# ----------------------------------------------------------- cached embedder


class FixtureCacheMissError(RuntimeError):
    """Raised when `_CachedEmbedder` receives a text not present in the JSON.

    Strict-by-design (PRD-045): a miss almost always means dataset / DB seed
    / chunk renderer drift. Silent fallback would hide the cause.
    """


class _CachedEmbedder:
    """Deterministic embedder. Replays production OpenAI embeddings.

    PRD-045: `bin/regenerate_embedding_cache.py` pre-fetches vectors for
    the 30 queries + 218 chunk descriptions and commits them as JSON.
    This class loads that JSON once per instance and returns the cached
    vector for each input text.
    """

    def __init__(self, cache_path: Path = CACHE_FILE) -> None:
        if not cache_path.exists():
            raise FixtureCacheMissError(
                f"embedding cache fixture missing: {cache_path}. "
                f"Regenerate with: OPENAI_API_KEY=... PG_DSN=... "
                f"uv run python bin/regenerate_embedding_cache.py"
            )
        with cache_path.open(encoding="utf-8") as fh:
            raw: dict[str, list[float]] = json.load(fh)
        self._cache: dict[str, tuple[float, ...]] = {
            k: tuple(v) for k, v in raw.items()
        }

    async def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in texts if t not in self._cache]
        if missing:
            raise FixtureCacheMissError(
                f"embedding_cache.json missing {len(missing)} of "
                f"{len(texts)} texts. First miss: {missing[0][:120]!r}. "
                f"Regenerate with: OPENAI_API_KEY=... PG_DSN=... "
                f"uv run python bin/regenerate_embedding_cache.py"
            )
        return [list(self._cache[t]) for t in texts]


# ----------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def app_engine(app_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(app_dsn, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_session(app_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(app_engine, expire_on_commit=False) as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def _seeded_index(app_engine: AsyncEngine) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Reindex with the cached embedder before every test in this file.

    The schema-indexer integration tests TRUNCATE the table, so we cannot
    rely on prior state. We re-seed here so the retriever tests see a
    known population every time.
    """
    async with app_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE pyrene_schema_embeddings"))

    async with AsyncSession(app_engine, expire_on_commit=False) as session:
        indexer = SchemaIndexer(
            write_session=session,
            embedder=_CachedEmbedder(),
        )
        await indexer.index_all()

    yield


# ------------------------------------------------------------ retriever shape


# Query texts used by the shape tests below. Both come from the dataset
# (`schema_retrieval_30.yaml` agg-03) so they're covered by the cache
# fixture without needing maintenance of a separate ad-hoc query list.
_PAYMENT_QUERY = "월별 결제 총액 추이"  # agg-03, expected_tables: [payment]


async def test_pgvector_retriever_returns_top_k_in_distance_order(
    app_session: AsyncSession,
) -> None:
    """A direct keyword match must be in the top-3."""
    retriever = PgvectorRetriever(
        session=app_session,
        embedder=_CachedEmbedder(),
    )
    chunks = await retriever.top_k(_PAYMENT_QUERY, k=3)

    assert chunks, "retriever returned no chunks"
    assert len(chunks) == 3
    tables = [c.table for c in chunks]
    assert "payment" in tables, (
        f"'payment' missing from top-3 for revenue query; got {tables}"
    )


async def test_pgvector_retriever_returns_empty_when_no_rows(
    app_engine: AsyncEngine,
) -> None:
    """If the index is empty (post-truncate, pre-reseed), top-k must return ()."""
    async with app_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE pyrene_schema_embeddings"))

    async with AsyncSession(app_engine, expire_on_commit=False) as session:
        retriever = PgvectorRetriever(
            session=session, embedder=_CachedEmbedder()
        )
        chunks = await retriever.top_k(_PAYMENT_QUERY, k=3)
    assert chunks == ()


# -------------------------------------------------------- 30-query accuracy


def _load_dataset() -> list[dict[str, Any]]:
    with DATA_FILE.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    cases: list[dict[str, Any]] = loaded["cases"]
    assert len(cases) == 30, f"expected 30 cases, got {len(cases)}"
    return cases


async def test_top_3_accuracy_meets_95_percent_threshold(
    app_session: AsyncSession,
) -> None:
    """PRD-002 §6 / PRD-045 threshold. Below 95% → retrieval regression.

    PRD-044 measured production at 100% top-3 across 3 variants on the
    docker-compose Postgres (initdb + alembic-migrated `pyrene_*` tables,
    218 chunks total). The testcontainers env in this fixture has only
    the initdb subset (111 chunks — no audit log, budget, etc.), so the
    cosine landscape differs slightly and observed recall is ~96.7%
    rather than 100%. Either way the 95% threshold protects the contract.

    We deliberately fail the test (no skip, no xfail) so a regression
    triggers PLAN-002 §91 (Day 2 reopened, chunk strategy revisited).
    """
    retriever = PgvectorRetriever(
        session=app_session, embedder=_CachedEmbedder()
    )

    cases = _load_dataset()
    hits = 0
    misses: list[tuple[str, str, list[str], list[str]]] = []
    for case in cases:
        query: str = case["query"]
        expected: list[str] = list(case["expected_tables"])
        chunks = await retriever.top_k(query, k=3)
        got = [c.table for c in chunks]
        if any(t in got for t in expected):
            hits += 1
        else:
            misses.append((case["id"], query, expected, got))

    accuracy = hits / len(cases)
    # Surface the accuracy figure in pytest output (-s) so PR reviewers and CI
    # logs see the exact margin, not just "PASSED".
    print(f"\nschema-RAG top-3 accuracy: {accuracy:.1%} ({hits}/{len(cases)})")
    # Format a compact failure report so the operator can see which queries
    # missed and pick the right L-03 follow-up (chunk-per-column, manual
    # description tweaks, etc.).
    miss_lines = [
        f"  - [{cid}] '{q}' expected one of {exp}, got top-3 {got}"
        for cid, q, exp, got in misses
    ]
    miss_report = "\n".join(miss_lines) or "  (none)"

    assert accuracy >= 0.95, (
        f"top-3 retrieval accuracy {accuracy:.1%} < 95% threshold "
        f"({hits}/{len(cases)} hits). PRD-002 L-03 escalation: revisit "
        f"chunk strategy or ef_search. PRD-045 cache → expected 100%. "
        f"Misses:\n{miss_report}"
    )
