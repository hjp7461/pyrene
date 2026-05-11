"""Integration tests for the 0002_agent_registry migration.

Validates:
  - `agent_specs` + `agent_versions` tables exist at head.
  - The 0002 INSERT-only role REVOKE is a no-op when pyrene_app role doesn't
    exist (testcontainers connects as superuser).
  - Round-trip: upgrade head → downgrade -1 → upgrade head succeeds.

ADR-013 (e) forward-only operationally + round-trip in tests.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def test_agent_tables_present_after_upgrade_head(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "agent_specs" in result
    assert "agent_versions" in result


async def test_agent_versions_unique_constraint(engine: AsyncEngine) -> None:
    """The (agent_id, version) UNIQUE constraint must be present."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                WHERE cls.relname = 'agent_versions'
                  AND con.contype = 'u'
                """
            )
        )
        names = {row[0] for row in rows}
    assert "uq_agent_versions_agent_version" in names


def test_round_trip_downgrade_upgrade(alembic_config: Config) -> None:
    """`upgrade head` → `downgrade -1` → `upgrade head` succeeds.

    ADR-013 (e) round-trip CI policy. The downgrade walks back exactly one
    revision (0002 → 0001_auth) and a re-upgrade brings 0002 back. If the
    migration's `downgrade()` is broken, the next `upgrade(head)` would
    fail with a duplicate-table error.
    """
    # The session-scoped migrated_db fixture left the DB at head; we
    # downgrade once and re-upgrade. Order matters for graph integrity.
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, "head")
