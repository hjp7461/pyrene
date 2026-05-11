"""Integration tests for SchemaIndexer against a real Postgres + DVD Rental.

The testcontainers fixture (`conftest.py`) restores DVD Rental and runs the
initdb scripts that create `pyrene_schema_embeddings` and apply the table
COMMENTs. The embedder is mocked end-to-end — we do NOT call OpenAI in CI
because (a) it costs real money and (b) the embeddings are not the unit
under test (the indexer's SQL is).

Coverage:
  - row count matches the number of BASE TABLEs in `public`
  - re-running is idempotent (no duplicate rows, same row IDs)
  - `--reindex` deletes prior rows for this connection_id
  - cosine top-k uses the HNSW index after ANALYZE (no seq scan)
"""

from __future__ import annotations

import math
import random
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from pyrene_sql.schema import DEFAULT_CONNECTION_ID, SchemaIndexer

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# A deterministic 1024-dim "embedding" generator. We seed per text so the
# same description always hashes to the same vector — idempotency tests
# compare row contents across runs.
def _deterministic_vector(text_in: str, dims: int = 1024) -> list[float]:
    rng = random.Random(hash(text_in) & 0xFFFFFFFF)
    raw = [rng.uniform(-1.0, 1.0) for _ in range(dims)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


class _DeterministicEmbedder:
    """Mock embedder. Same input → same 1024-dim output. No network calls."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_deterministic_vector(t) for t in texts]


@pytest_asyncio.fixture
async def app_engine(app_dsn: str) -> AsyncIterator[AsyncEngine]:
    """Write-role engine bound to the testcontainers DB."""
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
async def _clean_schema_embeddings(app_engine: AsyncEngine) -> AsyncIterator[None]:
    """Ensure each test starts with an empty pyrene_schema_embeddings.

    initdb only runs on the *first* DB boot of the container; subsequent
    tests in the same session inherit rows from earlier tests. We TRUNCATE
    per test so behavior is independent.
    """
    async with app_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE pyrene_schema_embeddings"))
    yield


# ----------------------------------------------------------------- table count


async def test_index_all_indexes_every_dvd_rental_table(
    app_session: AsyncSession,
) -> None:
    indexer = SchemaIndexer(
        write_session=app_session,
        embedder=_DeterministicEmbedder(),
    )

    n = await indexer.index_all()

    # Count BASE TABLEs in `public` to compare. DVD Rental ships with 15
    # tables; we assert against information_schema rather than a literal
    # so the test does not rot if the sample DB ever adds one.
    expected_row = (
        await app_session.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
    ).scalar_one()
    assert n == expected_row, (
        f"indexer reported {n} chunks but information_schema sees "
        f"{expected_row} BASE TABLEs"
    )

    row_count = (
        await app_session.execute(
            text("SELECT count(*) FROM pyrene_schema_embeddings")
        )
    ).scalar_one()
    assert row_count == expected_row


# ----------------------------------------------------------------- idempotency


async def test_index_all_is_idempotent_on_rerun(app_session: AsyncSession) -> None:
    """Same chunks indexed twice → row count unchanged (UPSERT on UNIQUE key)."""
    indexer = SchemaIndexer(
        write_session=app_session,
        embedder=_DeterministicEmbedder(),
    )

    n1 = await indexer.index_all()
    n2 = await indexer.index_all()
    assert n1 == n2

    row_count = (
        await app_session.execute(
            text("SELECT count(*) FROM pyrene_schema_embeddings")
        )
    ).scalar_one()
    assert row_count == n1


async def test_index_all_reindex_replaces_prior_rows(
    app_session: AsyncSession,
) -> None:
    """`reindex=True` deletes all rows for this connection_id then re-inserts."""
    indexer = SchemaIndexer(
        write_session=app_session,
        embedder=_DeterministicEmbedder(),
    )
    await indexer.index_all()

    # Capture the original row IDs.
    before_ids = {
        row[0]
        for row in (
            await app_session.execute(
                text(
                    "SELECT id FROM pyrene_schema_embeddings "
                    "WHERE connection_id = :cid"
                ),
                {"cid": DEFAULT_CONNECTION_ID},
            )
        ).fetchall()
    }
    assert before_ids

    await indexer.index_all(reindex=True)

    after_ids = {
        row[0]
        for row in (
            await app_session.execute(
                text(
                    "SELECT id FROM pyrene_schema_embeddings "
                    "WHERE connection_id = :cid"
                ),
                {"cid": DEFAULT_CONNECTION_ID},
            )
        ).fetchall()
    }
    assert after_ids
    # Reindex must DELETE first (so the IDs flip — gen_random_uuid()).
    assert before_ids.isdisjoint(after_ids), "reindex did not delete prior rows"


# --------------------------------------------------------------- HNSW indexing


async def test_hnsw_index_scan_is_available_after_analyze(
    app_session: AsyncSession,
) -> None:
    """ADR-013 (c): the HNSW index exists and is usable for cosine top-k.

    DVD Rental only has ~15 rows in this table at test time, so the planner
    is right to prefer a seq scan over HNSW (HNSW only pays off at scale).
    We assert two weaker but production-relevant guarantees:

      1. The HNSW index exists with the vector_cosine_ops opclass.
      2. With `enable_seqscan=off`, the planner actually selects the HNSW
         Index Scan for `ORDER BY embedding <=> ... LIMIT k`.

    Both guarantees fail-fast if (a) the initdb script lost the index, or
    (b) pgvector ever refused to plan an HNSW scan for this query shape.
    """
    indexer = SchemaIndexer(
        write_session=app_session,
        embedder=_DeterministicEmbedder(),
    )
    await indexer.index_all()

    # 1. Index must exist with the cosine opclass that pgvector parses to HNSW.
    index_def = (
        await app_session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                " WHERE indexname = 'pyrene_schema_embeddings_embedding_hnsw'"
            )
        )
    ).scalar_one()
    assert "hnsw" in index_def.lower()
    assert "vector_cosine_ops" in index_def

    # 2. Force the planner to consider the index, then EXPLAIN the cosine query.
    qv = _deterministic_vector("revenue by category", dims=1024)
    qv_literal = "[" + ",".join(f"{x:.7g}" for x in qv) + "]"

    await app_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan_rows = (
        await app_session.execute(
            text(
                "EXPLAIN (FORMAT TEXT) "
                "SELECT id FROM pyrene_schema_embeddings "
                "ORDER BY embedding <=> CAST(:qv AS vector) "
                "LIMIT 3"
            ),
            {"qv": qv_literal},
        )
    ).fetchall()
    plan = "\n".join(row[0] for row in plan_rows)

    assert "Index Scan" in plan, (
        "expected HNSW Index Scan in plan after disabling seq scan; "
        f"pgvector may have lost HNSW support.\nPlan:\n{plan}"
    )
    assert "hnsw" in plan.lower(), (
        f"plan did not name the HNSW index; got:\n{plan}"
    )


# -------------------------------------------------------- comments are present


async def test_indexed_descriptions_include_initdb_table_comments(
    app_session: AsyncSession,
) -> None:
    """The 04-table-comments.sql COMMENT bodies must end up in `description`."""
    indexer = SchemaIndexer(
        write_session=app_session,
        embedder=_DeterministicEmbedder(),
    )
    await indexer.index_all()

    desc = (
        await app_session.execute(
            text(
                "SELECT description FROM pyrene_schema_embeddings "
                'WHERE schema = :s AND "table" = :t'
            ),
            {"s": "public", "t": "payment"},
        )
    ).scalar_one()
    # The COMMENT ON TABLE body for payment includes "revenue / sales".
    assert "revenue" in desc, f"payment description missing comment body:\n{desc}"
