"""Integration test fixtures: testcontainers Postgres + DVD Rental seed.

Spins up `pgvector/pgvector:pg16` with the same initdb scripts the dev
docker-compose uses, so the read-only role and DVD Rental data are present
exactly as in production. Session-scoped because container boot + restore is
expensive (~10-20s); function-level isolation is unnecessary for the tests
in this file (they're read-only or DDL/DML rejection cases that don't mutate).
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[4]
INITDB_DIR = REPO_ROOT / "deploy" / "postgres" / "initdb"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    if not _docker_available():
        pytest.skip("docker not available; integration tests skipped")
    if not (INITDB_DIR / "dvdrental.tar").exists():
        pytest.skip("dvdrental.tar missing; integration tests skipped")

    container = (
        PostgresContainer(
            image="pgvector/pgvector:pg16",
            username="pyrene",
            password="pyrene",
            dbname="dvdrental",
        )
        .with_volume_mapping(
            str(INITDB_DIR),
            "/docker-entrypoint-initdb.d",
            mode="ro",
        )
    )
    with container as ctx:
        yield ctx


@pytest.fixture(scope="session")
def app_dsn(postgres_container: PostgresContainer) -> str:
    """asyncpg DSN bound to the application (write) role."""
    raw: str = postgres_container.get_connection_url()
    # testcontainers returns 'postgresql+psycopg2://...' by default; rewrite to asyncpg.
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture(scope="session")
def readonly_dsn(postgres_container: PostgresContainer) -> str:
    """asyncpg DSN bound to the `pyrene_readonly` role created by initdb."""
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return f"postgresql+asyncpg://pyrene_readonly:readonly@{host}:{port}/dvdrental"


@pytest_asyncio.fixture
async def readonly_engine(readonly_dsn: str) -> AsyncIterator[AsyncEngine]:
    # Function-scoped + NullPool: each test gets its own asyncpg connection bound
    # to the current event loop. Avoids the "future attached to a different loop"
    # error that arises when pytest-asyncio's per-test loop reuses a pooled
    # connection from a prior loop.
    engine = create_async_engine(readonly_dsn, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def readonly_session(readonly_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(readonly_engine, expire_on_commit=False) as session:
        yield session
