#!/usr/bin/env python
"""Regenerate the deterministic embedding cache fixture for retriever tests.

PRD-045 — `_CachedEmbedder` (in `packages/pyrene-sql/tests/integration/
test_retriever_db.py`) replays production OpenAI `text-embedding-3-small`
vectors from `packages/pyrene-sql/tests/data/embedding_cache.json`. This
script regenerates that JSON.

Why testcontainers, not docker compose:
    The integration tests' conftest (`packages/pyrene-sql/tests/integration/
    conftest.py`) spins up `pgvector/pgvector:pg16` with ONLY the initdb
    scripts (DVD Rental + pyrene_schema_embeddings DDL). The local
    docker-compose Postgres has alembic-migrated tables on top
    (pyrene_audit_log, pyrene_budget_*, etc.) which the test env does not.
    Running regen against docker-compose would produce 218 chunks vs the
    111 the test env emits, causing every CI run to miss the cache.

    This script spins up its own ephemeral testcontainers Postgres with
    the *same* image + initdb mount that conftest uses, so the cache is
    byte-aligned to what the tests will request.

When to regenerate:
- `schema_retrieval_30.yaml` query texts changed
- DVD Rental seed (`deploy/postgres/initdb/*`) changed → chunk texts shift
- `SchemaIndexer.render_chunk_description` /
  `render_column_chunk_description` output changed
- OpenAI model swap (e.g. text-embedding-3-large) — would also need
  EMBEDDING_DIMENSIONS adjustment + Alembic vector(N) migration

Usage:
    OPENAI_API_KEY=sk-... uv run python bin/regenerate_embedding_cache.py

Output:
    packages/pyrene-sql/tests/data/embedding_cache.json (~1.4MB, 111 entries)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from pyrene_sql.schema.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingClient,
    OpenAIEmbedder,
)
from pyrene_sql.schema.indexer import SchemaIndexer

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INITDB_DIR = _REPO_ROOT / "deploy" / "postgres" / "initdb"
_DATASET = (
    _REPO_ROOT
    / "packages"
    / "pyrene-sql"
    / "tests"
    / "data"
    / "schema_retrieval_30.yaml"
)
_CACHE_OUT = (
    _REPO_ROOT
    / "packages"
    / "pyrene-sql"
    / "tests"
    / "data"
    / "embedding_cache.json"
)


class _UnusedEmbedder:
    """Stub embedder injected into SchemaIndexer for chunk-text extraction only.

    `SchemaIndexer.__init__` requires an EmbeddingClient even though
    `_build_chunks()` never calls `embed`. We raise loudly if anything
    in this script accidentally triggers an embedding call — that would
    indicate a refactor of SchemaIndexer worth re-reading before regen.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise RuntimeError(
            "_UnusedEmbedder.embed must not be called from this regen script."
        )


def _load_query_texts(path: Path) -> list[str]:
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    cases: list[dict[str, str]] = loaded["cases"]
    return [case["query"] for case in cases]


async def _collect_chunk_texts(session: AsyncSession) -> list[str]:
    """Run SchemaIndexer._build_chunks against the live DB and return the
    list of chunk descriptions (table chunks + column chunks).
    """
    embedder: EmbeddingClient = _UnusedEmbedder()
    indexer = SchemaIndexer(write_session=session, embedder=embedder)
    # `_build_chunks` is the private helper that does information_schema
    # queries and renders text. We intentionally bypass `index_all()` so
    # nothing writes to `pyrene_schema_embeddings` from this script.
    chunks = await indexer._build_chunks()
    return [chunk.description for chunk in chunks]


def _serialize_cache(cache: dict[str, list[float]], path: Path) -> int:
    """Write the cache as JSON with `{:.7g}` precision per float.

    `json.dump` would emit Python `repr(float)` = 17 sig figs, bloating the
    file ~2x without information gain (`text-embedding-3-small` is float32
    internally). Match the pgvector literal precision in
    `schema/indexer.py:_upsert` (`{:.7g}`).
    """
    import json as _json

    parts: list[str] = ["{\n"]
    items = list(cache.items())
    for i, (chunk_or_query_text, vec) in enumerate(items):
        key = _json.dumps(chunk_or_query_text, ensure_ascii=False)
        vec_str = "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
        comma = "," if i < len(items) - 1 else ""
        parts.append(f"  {key}: {vec_str}{comma}\n")
    parts.append("}\n")
    content = "".join(parts)
    path.write_text(content, encoding="utf-8")
    return len(content.encode("utf-8"))


def _check_docker_available() -> int | None:
    """Return non-None exit code if docker is unusable."""
    if shutil.which("docker") is None:
        print("ERROR: docker not available in PATH", file=sys.stderr)
        return 3
    if not (_INITDB_DIR / "dvdrental.tar").exists():
        print(f"ERROR: dvdrental.tar missing in {_INITDB_DIR}", file=sys.stderr)
        return 3
    return None


def _container_dsn(container: PostgresContainer) -> str:
    raw: str = container.get_connection_url()
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


async def _embed_and_write(
    pg_dsn: str,
    query_texts: list[str],
    args: argparse.Namespace,
    openai_key: str,
) -> int:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            chunk_texts = await _collect_chunk_texts(session)
    finally:
        await engine.dispose()
    print(f"chunks extracted: {len(chunk_texts)} (table + column)")

    # Dedup before embedding (cache key collisions = 0 cost), preserve order
    # for stable JSON layout. dict.fromkeys keeps first-seen order, ≥ 3.7.
    seen: dict[str, None] = {}
    for t in chunk_texts + query_texts:
        seen.setdefault(t, None)
    unique_texts = list(seen.keys())
    dup_count = (len(chunk_texts) + len(query_texts)) - len(unique_texts)
    if dup_count:
        print(
            f"deduplicated: {dup_count} text(s) appear in both chunks and queries"
        )
    print(f"unique texts to embed: {len(unique_texts)}")

    embedder = OpenAIEmbedder(api_key=openai_key)
    print(f"embedding via {embedder._model} @ {EMBEDDING_DIMENSIONS} dims …")
    vectors = await embedder.embed(unique_texts)
    if len(vectors) != len(unique_texts):
        print(
            f"ERROR: embedder returned {len(vectors)} vectors for "
            f"{len(unique_texts)} inputs",
            file=sys.stderr,
        )
        return 4
    for i, vec in enumerate(vectors):
        if len(vec) != EMBEDDING_DIMENSIONS:
            print(
                f"ERROR: vector[{i}] has {len(vec)} dims, expected "
                f"{EMBEDDING_DIMENSIONS}",
                file=sys.stderr,
            )
            return 4

    cache: dict[str, list[float]] = dict(
        zip(unique_texts, vectors, strict=True)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = _serialize_cache(cache, args.output)
    print(
        f"\nwrote {len(cache)} entries · "
        f"{bytes_written / 1024:.1f} KiB -> {args.output}"
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        print("ERROR: OPENAI_API_KEY env var required", file=sys.stderr)
        return 2

    if not args.dataset.exists():
        print(f"ERROR: dataset not found: {args.dataset}", file=sys.stderr)
        return 3

    docker_err = _check_docker_available()
    if docker_err is not None:
        return docker_err

    query_texts = _load_query_texts(args.dataset)
    print(f"queries loaded: {len(query_texts)} from {args.dataset.name}")

    print(
        f"starting testcontainers pgvector/pgvector:pg16 "
        f"(initdb mount: {_INITDB_DIR.name}/) …"
    )
    container = PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="pyrene",
        password="pyrene",
        dbname="dvdrental",
    ).with_volume_mapping(
        str(_INITDB_DIR),
        "/docker-entrypoint-initdb.d",
        mode="ro",
    )
    with container as ctx:
        pg_dsn = _container_dsn(ctx)
        return await _embed_and_write(pg_dsn, query_texts, args, openai_key)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=_DATASET,
        help="Path to the 30-case yaml dataset",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_CACHE_OUT,
        help="Path to write the embedding cache JSON",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
