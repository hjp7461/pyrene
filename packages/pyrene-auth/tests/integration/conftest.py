"""Integration test fixtures for pyrene-auth (testcontainers Postgres).

ADR-014: function-scoped savepoint isolation on top of a session-scoped
container. The container boots once per pytest session; each test gets a
SAVEPOINT-wrapped AsyncSession that rolls back on exit.

ADR-013 (a): the global Alembic config (`alembic.ini` at repo root) is run
once at session start to bring the schema to head before any test runs.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    if not _docker_available():
        pytest.skip("docker not available; integration tests skipped")
    container = PostgresContainer(
        image="postgres:16-alpine",
        username="pyrene",
        password="pyrene",
        dbname="pyrene_auth_test",
    )
    with container as ctx:
        yield ctx


@pytest.fixture(scope="session")
def app_dsn(postgres_container: PostgresContainer) -> str:
    raw: str = postgres_container.get_connection_url()
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture(scope="session")
def alembic_config(app_dsn: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", app_dsn)
    # env.py also reads PG_DSN; set it for both paths.
    os.environ["PG_DSN"] = app_dsn
    return cfg


@pytest.fixture(scope="session")
def migrated_db(alembic_config: Config, app_dsn: str) -> Iterator[str]:
    """Brings the test DB to head once per session; tests share the schema."""
    command.upgrade(alembic_config, "head")
    yield app_dsn


@pytest_asyncio.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """ADR-014 savepoint-isolated session.

    Each test:
      1. BEGIN outer transaction
      2. SAVEPOINT (begin_nested)
      3. AsyncSession bound to the connection with
         `join_transaction_mode="create_savepoint"` so test code that calls
         `session.commit()` only releases the savepoint, not the outer txn.
      4. yield
      5. rollback savepoint + outer transaction → clean slate for next test.
    """
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            await conn.begin_nested()
            session_factory = async_sessionmaker(
                bind=conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with session_factory() as session:
                yield session
        finally:
            await outer.rollback()


@pytest_asyncio.fixture
async def raw_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Lower-level connection for migration tests that need DDL outside SAVEPOINT."""
    async with engine.connect() as conn:
        yield conn
