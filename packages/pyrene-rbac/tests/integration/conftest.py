"""Integration test fixtures for pyrene-rbac.

Differs from peer packages on one point: the Alembic chain at Wave 7
has THREE pending heads (0004_rbac_matrix from PLAN-010, 0005 from
PLAN-013, 0006 from PLAN-015) all chained off 0003 during isolated
development. Running `command.upgrade("head")` therefore fails with
"multiple heads".

We upgrade to OUR specific revision (`0004_rbac_matrix`) so this
package's integration tests are robust to the parallel-wave state.
The integration-branch PM rebases the chain at merge time (see
0005/0006 docstrings for the same note).
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
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Our PLAN's revision. Pinning is required because Wave 7 parallel work
# left multiple alembic heads (0004 + 0005 + 0006 all chain from 0003).
TARGET_REVISION = "0004_rbac_matrix"


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
        dbname="pyrene_rbac_test",
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
    os.environ["PG_DSN"] = app_dsn
    return cfg


@pytest.fixture(scope="session")
def migrated_db(alembic_config: Config, app_dsn: str) -> Iterator[str]:
    """Brings the test DB to the PLAN-010 revision (`0004_rbac_matrix`).

    NOT `head`: see module docstring. Phase 2 integration branch will
    re-pin once 0004 → 0005 → 0006 is linearized.
    """
    command.upgrade(alembic_config, TARGET_REVISION)
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
    """ADR-014 savepoint-isolated session — every test rolls back on exit."""
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
