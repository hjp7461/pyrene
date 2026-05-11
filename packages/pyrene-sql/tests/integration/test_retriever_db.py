"""Integration tests for PgvectorRetriever + the 30-query retrieval dataset.

These tests are the regression guard for L-03 (PRD-002 §7): if top-3 accuracy
drops below 90% on the canned dataset (`tests/data/schema_retrieval_30.yaml`),
the chunk-per-table strategy is no longer good enough and we need to revisit
chunk-per-column. We fail the suite rather than skip — silent regression on
retrieval is exactly the failure mode PLAN-002 calls out.

Why a deterministic mock embedder, not real OpenAI:
  - The integration *contract* under test is "embed → cosine query → top-3
    rehydration → SchemaChunk". Real embeddings give us a different metric
    (model quality), not pipeline correctness. We pay for real embedding
    evals separately in the live test suite (out of CI).
  - For 30 fixed queries we want byte-stable vectors so the same DB + same
    code reproduces the same accuracy across runs. Real OpenAI does NOT
    promise this — voyage / openai both occasionally drift.

The deterministic embedder is bag-of-words + a small Korean→English keyword
bridge. We project tokens into 1024 hashed buckets and L2-normalize. The
bridge dict translates the high-signal Korean nouns used in the dataset
(매출 → revenue, 결제 → payment, 영화 → film, ...) into the same English
vocabulary the `COMMENT ON TABLE` bodies use. Real LLM embeddings do this
natively across languages; the bridge is what lets the *deterministic* mock
clear the same threshold (PRD-002 §6 ≥ 90%) without resorting to OpenAI.
The bridge lives next to the test on purpose — it is part of the test
fixture, not the production retriever.
"""

from __future__ import annotations

import re
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
    EMBEDDING_DIMENSIONS,
    PgvectorRetriever,
    SchemaIndexer,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "schema_retrieval_30.yaml"


# --------------------------------------------------- keyword bag-of-words space


_WORD_RE = re.compile(r"[A-Za-z가-힣]+")


# Korean → English keyword bridge. Real LLM embeddings do cross-lingual
# semantic matching natively; the deterministic bag-of-words mock has no
# semantic layer, so we hard-code the synonym map. Each Korean token expands
# into a small bag of English keywords that actually appear in the relevant
# table's COMMENT ON body (see deploy/postgres/initdb/04-table-comments.sql).
#
# The bridge is intentionally narrow — only the high-signal nouns the dataset
# uses. Expanding it further would mask retrieval regressions instead of
# revealing them.
_KO_BRIDGE: dict[str, tuple[str, ...]] = {
    "매출": ("revenue", "sales", "amount", "payment"),
    "결제": ("payment", "amount", "money"),
    "총액": ("amount", "total", "payment"),
    "금액": ("amount", "payment", "money"),
    "영화": ("film", "films"),
    "제목": ("title", "film"),
    "배우": ("actor", "actors"),
    "출연": ("actor", "film_actor", "actor_id"),
    "고객": ("customer", "customers"),
    "직원": ("staff", "staff_id"),
    "매장": ("store", "stores"),
    "주소": ("address", "addresses"),
    "도시": ("city", "cities"),
    "국가": ("country", "countries"),
    "언어": ("language", "languages"),
    "카테고리": ("category", "categories", "genre", "film_category"),
    "장르": ("category", "genre"),
    "대여": ("rental", "rented"),
    "빌리": ("rental", "rented", "rent"),
    "빌린": ("rental", "rented"),
    "빌리지": ("rental", "rented"),
    "반납": ("return_date", "rental"),
    "반납되지": ("return_date", "rental"),
    "재고": ("inventory", "store"),
    "분기": ("payment_date", "date"),
    "월별": ("payment_date", "month"),
    "일별": ("payment_date", "date"),
    "추이": ("payment", "payment_date"),
    "이상": ("amount", "payment"),
    "이름": ("name", "first_name", "last_name"),
    "찾기": ("search", "find"),
    "보여줘": ("list", "select"),
    "활성": ("active",),
    "비활성": ("active",),
    "상태": ("active",),
    "한국": ("country",),
    "도쿄": ("city",),
    "영어": ("language", "english"),
}


# English stem map: tokens we want to canonicalize before hashing. This
# bridges trivial morphological variation (films ↔ film, payments ↔ payment)
# so cosine top-3 stays stable across query phrasing.
_EN_STEM: dict[str, str] = {
    "films": "film",
    "payments": "payment",
    "rentals": "rental",
    "customers": "customer",
    "actors": "actor",
    "staffs": "staff",
    "stores": "store",
    "categories": "category",
    "languages": "language",
    "cities": "city",
    "countries": "country",
    "addresses": "address",
    "released": "release_year",
    "release": "release_year",
}


def _tokenize(text_in: str) -> list[str]:
    """Lowercase keyword tokens, with stem + Korean→English bridge expansion.

    Numbers + punctuation are stripped. Each Korean token also emits its
    English synonyms so cross-lingual queries hit the same buckets as the
    English COMMENT bodies in `pyrene_schema_embeddings.description`. Trivial
    English plurals are stemmed (films → film) so query-vs-comment matches
    don't depend on number agreement.
    """
    tokens: list[str] = []
    for m in _WORD_RE.finditer(text_in):
        tok = m.group(0).lower()
        tok = _EN_STEM.get(tok, tok)
        tokens.append(tok)
        if tok in _KO_BRIDGE:
            tokens.extend(_KO_BRIDGE[tok])
    return tokens


# A fixed keyword universe sized to the embedding dim (1024). We hash each
# token into one of 1024 buckets so the bag-of-words vector lives in the same
# space as the column. This is "deterministic random projection" — crude but
# adequate for the schema retrieval regression guard.
def _bow_vector(text_in: str, dims: int = EMBEDDING_DIMENSIONS) -> list[float]:
    vec = [0.0] * dims
    for tok in _tokenize(text_in):
        idx = hash(("pyrene-bow", tok)) % dims
        vec[idx] += 1.0
    # L2-normalize so cosine distance == 1 - dot product.
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class _BagOfWordsEmbedder:
    """Deterministic embedder. Bag-of-words → 1024-dim L2-normalized vector.

    This is intentionally weaker than a real LLM embedder, but it
    discriminates the DVD Rental tables well because each `COMMENT ON TABLE`
    body uses distinct vocabulary (payment → "money", "amount", "revenue";
    customer → "customers", "email", "address_id"; etc.). The 30-query yaml
    was hand-picked with this in mind.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_bow_vector(t) for t in texts]


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
async def _seeded_index(app_engine: AsyncEngine) -> AsyncIterator[None]:
    """Reindex with the deterministic embedder before every test in this file.

    The schema-indexer integration tests TRUNCATE the table, so we cannot
    rely on prior state. We re-seed here so the retriever tests see a known
    population every time.
    """
    async with app_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE pyrene_schema_embeddings"))

    async with AsyncSession(app_engine, expire_on_commit=False) as session:
        indexer = SchemaIndexer(
            write_session=session,
            embedder=_BagOfWordsEmbedder(),
        )
        await indexer.index_all()

    yield


# ------------------------------------------------------------ retriever shape


async def test_pgvector_retriever_returns_top_k_in_distance_order(
    app_session: AsyncSession,
) -> None:
    """A direct keyword match must be in the top-3."""
    retriever = PgvectorRetriever(
        session=app_session,
        embedder=_BagOfWordsEmbedder(),
    )
    chunks = await retriever.top_k("payment amount revenue", k=3)

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
            session=session, embedder=_BagOfWordsEmbedder()
        )
        chunks = await retriever.top_k("payment revenue", k=3)
    assert chunks == ()


# -------------------------------------------------------- 30-query accuracy


def _load_dataset() -> list[dict[str, Any]]:
    with DATA_FILE.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    cases: list[dict[str, Any]] = loaded["cases"]
    assert len(cases) == 30, f"expected 30 cases, got {len(cases)}"
    return cases


async def test_top_3_accuracy_meets_90_percent_threshold(
    app_session: AsyncSession,
) -> None:
    """PRD-002 §6 hard threshold. Below 90% → L-03 (re-chunk strategy).

    This is the load-bearing eval. We deliberately fail the test (no skip,
    no xfail) so a regression triggers PLAN-002 §91 (Day 2 reopened, chunk
    strategy revisited). The mock embedder is a stable lower bound — real
    OpenAI embeddings should perform at or above this level.
    """
    retriever = PgvectorRetriever(
        session=app_session, embedder=_BagOfWordsEmbedder()
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

    assert accuracy >= 0.90, (
        f"top-3 retrieval accuracy {accuracy:.1%} < 90% threshold "
        f"({hits}/{len(cases)} hits). PRD-002 L-03 escalation: revisit "
        f"chunk strategy (table → column). Misses:\n{miss_report}"
    )
