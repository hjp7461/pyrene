"""Integration test fixtures for pyrene-metering.

Mirrors the pyrene-gateway / pyrene-auth pattern: a session-scoped
testcontainers Postgres + Alembic upgrade head, and per-function
SAVEPOINT isolation per ADR-014.
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
        dbname="pyrene_metering_test",
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
    """Brings the DB to the metering head once per session.

    Wave 7 reality: there are multiple heads (0004_rbac_matrix,
    0005_cost_metering, 0006_audit_log) divergent off 0003 because
    peer PLANs developed in isolation. We upgrade only our chain head
    by referencing the explicit revision so this branch's tests do
    not need PLAN-010 / PLAN-015 tables.
    """
    command.upgrade(alembic_config, "0005_cost_metering")
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
async def seeded_user_team(db_session: AsyncSession) -> tuple[UUID, UUID]:
    """Insert one user + one team that integration tests can FK against.

    Returns `(user_id, team_id)`. Both are uuid4 — collision-free per test
    (per-function fixture). Uses raw SQL so we don't import pyrene_auth
    models (Wave 7 guardrail: pyrene-metering's tests stay package-local).
    """
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
