"""Integration test fixtures for pyrene-budget.

Mirrors `pyrene-metering/tests/integration/conftest.py` (PLAN-013 pattern):
session-scoped testcontainers Postgres + Alembic upgrade to the budget
head, plus per-function SAVEPOINT isolation (ADR-014).

Test naming: every file in this directory ends in `_budget.py` per the
Wave 8 guardrail (unique naming across parallel PLANs).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
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
        dbname="pyrene_budget_test",
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
    """Upgrade to this PLAN's head (`0008_budget_limits`).

    Wave 8 reality: the Alembic chain has multiple heads on disk
    (`0006_audit_log`, `0007_data_permissions`, `0008_budget_limits`)
    until landing rebases pin them linear. We explicitly request
    `0008_budget_limits`; Alembic walks the linked-list and applies
    every ancestor — `0001` through `0008` — in order.
    """
    command.upgrade(alembic_config, "0008_budget_limits")
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
    """ADR-014 savepoint-isolated session."""
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
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the real engine (no savepoint).

    Used by the advisory-lock concurrency test where two real
    transactions must contend on the same lock — savepoint isolation
    would collapse them onto the same TXN.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def seeded_user_team(db_session: AsyncSession) -> tuple[UUID, UUID]:
    """Insert one user + one team (raw SQL — no pyrene_auth model import)."""
    user_id = uuid4()
    team_id = uuid4()
    await db_session.execute(
        text(
            "INSERT INTO users (id, email, password_hash, is_active) "
            "VALUES (:id, :email, :pw, TRUE)"
        ),
        {"id": user_id, "email": f"user-{user_id}@example.test", "pw": "x"},
    )
    await db_session.execute(
        text("INSERT INTO teams (id, name) VALUES (:id, :name)"),
        {"id": team_id, "name": f"team-{team_id}"},
    )
    await db_session.flush()
    return user_id, team_id
