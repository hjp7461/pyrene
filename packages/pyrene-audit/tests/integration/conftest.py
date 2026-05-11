"""Integration test fixtures for pyrene-audit.

Mirrors `pyrene-auth` / `pyrene-gateway` conftest layout. Reuses the
global Alembic config (`migrations/env.py`) so the chain
0001 → 0002 → 0003 → 0004 → 0005 → 0006 runs to head before tests.

ADR-014 savepoint isolation interaction with WORM:
  - `db_session` uses `join_transaction_mode="create_savepoint"` so
    test commits release a savepoint and the outer rollback wipes the
    test data.
  - For audit tests we additionally provide `bypass_session` which
    sets `SET LOCAL audit.bypass = 'on'` for super-role cleanup
    operations. The WORM trigger checks the GUC at statement time;
    rolling back the outer txn resets the GUC automatically.
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

# Wave 7 chain reality: PLAN-010 (0004) and PLAN-013 (0005) currently
# both declare `down_revision = 0003_mcp_gateway`, creating two heads
# (0004 and 0006). Until the PM coordinator rebases 0005 → 0004,
# `command.upgrade("head")` errors with "Multiple head revisions". Each
# parallel package therefore targets its OWN revision explicitly; the
# integration branch flips to "head" once 0005's down_revision lands
# on 0004 (single-PR rebase, no code change here).
TARGET_REVISION = "0006_audit_log"


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
        dbname="pyrene_audit_test",
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
    """Brings the test DB up to `TARGET_REVISION` once per session.

    See `TARGET_REVISION` comment above — Wave 7 has multiple heads
    in flight; we target our own revision explicitly. Because
    `0006_audit_log.down_revision = 0005_cost_metering`, this picks up
    every prior migration in the linear ancestry (0001..0006 along the
    audit branch).
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
    """ADR-014 savepoint-isolated session.

    Compatible with the WORM trigger: the trigger fires on UPDATE/DELETE/
    TRUNCATE, none of which the savepoint-clean-slate path issues — the
    outer rollback uses connection-level state, not row-level
    statements.
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
    """Lower-level connection — required for WORM matrix tests that need
    to issue UPDATE/DELETE/TRUNCATE outside the savepoint sandbox so the
    error path is exercised against a real INSERT-then-mutate sequence.
    """
    async with engine.connect() as conn:
        yield conn
        # Best-effort cleanup of any test rows: super-role bypass for
        # local cleanup. Outside CI we just leave the rows in place
        # since the schema is dropped with the container.
        try:
            await conn.execute(text("SET LOCAL audit.bypass = 'on'"))
            await conn.execute(text("DELETE FROM audit_events"))
            await conn.commit()
        except Exception:  # pragma: no cover — best-effort
            await conn.rollback()


@pytest_asyncio.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real `async_sessionmaker` for the DBAuditSink integration tests."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
