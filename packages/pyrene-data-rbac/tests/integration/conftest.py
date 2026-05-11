"""Integration test fixtures for pyrene-data-rbac.

Wave 8 chain reality: PLAN-011 (this PLAN, 0007), PLAN-012, PLAN-014
are landing in parallel. We pin to OUR specific revision so this
package's integration tests are robust to the parallel-wave state.

The integration-branch PM rebases the chain at merge time (mirrors
the wave-7 pattern in `pyrene-rbac/tests/integration/conftest.py`).

WORM compatibility note: `audit_events` (introduced in 0006_audit_log,
a parent of this revision) is WORM-guarded. Cleanup paths that need
to truncate audit rows MUST first issue
`SET LOCAL audit.bypass = 'on'` (super-role bypass) — the test
sessions here do not write to `audit_events`, but the savepoint
rollback handles any incidental writes.
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
from sqlalchemy import text
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

# Wave 8 parallel: pin to our revision rather than "head" (multiple
# heads in flight until PM coordinator rebases).
TARGET_REVISION = "0007_data_permissions"


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
        dbname="pyrene_data_rbac_test",
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
    """Bring the test DB to the PLAN-011 revision (`0007_data_permissions`).

    NOT `head`: Wave 8 has multiple heads in flight. The integration
    branch will re-pin once 0007 + 0008 are linearized.
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
            # WORM-compat: set the bypass GUC inside the outer txn so
            # any incidental audit writes (cleanup paths) inherit the
            # bypass. `SET LOCAL` is scoped to the surrounding txn, so
            # the outer rollback resets it automatically (Wave 7 pattern
            # used by `pyrene-audit/tests/integration/conftest.py`).
            await conn.execute(text("SET LOCAL audit.bypass = 'on'"))
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
    """Lower-level connection — used by the dual-defense / direct-SQL
    bypass tests that need to step outside the savepoint sandbox.
    """
    async with engine.connect() as conn:
        yield conn
        try:
            await conn.execute(text("SET LOCAL audit.bypass = 'on'"))
            await conn.rollback()
        except Exception:  # pragma: no cover — best-effort
            pass
