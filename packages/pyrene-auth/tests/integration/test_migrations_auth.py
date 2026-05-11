"""Migration round-trip test (ADR-013 (e)).

Operations production policy is forward-only, but the test environment must
prove that `downgrade -1 → upgrade head` is a no-op so Alembic graph
integrity is verifiable at PR time. We also assert that the post-upgrade
schema contains the expected tables and that the `user_team_roles` FK
cascade is `CASCADE` at the live DB layer (not just in SQLAlchemy metadata).
"""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def _fetch_tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public'"
            )
        )
        return {row[0] for row in result}


async def _fetch_fk_actions(engine: AsyncEngine, table: str) -> dict[str, str]:
    """Return {column_name: delete_rule} for every FK on `table`."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT kcu.column_name, rc.delete_rule
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                  JOIN information_schema.referential_constraints rc
                    ON tc.constraint_name = rc.constraint_name
                 WHERE tc.constraint_type = 'FOREIGN KEY'
                   AND tc.table_name = :tbl
                """
            ),
            {"tbl": table},
        )
        return {row[0]: row[1] for row in result}


async def test_migrations_upgrade_head_creates_all_tables(
    migrated_db: str, engine: AsyncEngine
) -> None:
    tables = await _fetch_tables(engine)
    assert {"users", "teams", "roles", "user_team_roles"}.issubset(tables)


async def test_user_team_role_fk_cascade_at_db_layer(engine: AsyncEngine) -> None:
    """ADR-013 (b): all three FKs must be CASCADE in the live DB."""
    actions = await _fetch_fk_actions(engine, "user_team_roles")
    assert actions["user_id"] == "CASCADE"
    assert actions["team_id"] == "CASCADE"
    assert actions["role_id"] == "CASCADE"


async def test_migrations_round_trip(alembic_config: Config, engine: AsyncEngine) -> None:
    """downgrade to base → upgrade head must be idempotent (ADR-013 (e)).

    Alembic's `command.{up,down}grade` internally calls `asyncio.run(...)`
    via our env.py. Running it inside this async test would conflict with
    pytest-asyncio's loop — wrap in `asyncio.to_thread` so alembic owns its
    own event loop in a worker thread.

    PLAN-008: now that 0002 (agent_registry) is on the chain, we downgrade
    to `base` (drops all migrations) instead of `-1` so the auth tables
    actually disappear. The round-trip check still proves graph integrity.
    """
    await asyncio.to_thread(command.downgrade, alembic_config, "base")
    tables_after_down = await _fetch_tables(engine)
    assert "users" not in tables_after_down
    assert "user_team_roles" not in tables_after_down
    assert "agent_specs" not in tables_after_down

    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    tables_after_up = await _fetch_tables(engine)
    assert {"users", "teams", "roles", "user_team_roles"}.issubset(tables_after_up)
    assert {"agent_specs", "agent_versions"}.issubset(tables_after_up)
