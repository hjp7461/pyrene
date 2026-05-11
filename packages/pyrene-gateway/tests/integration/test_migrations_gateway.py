"""Integration tests for the 0003_mcp_gateway migration.

Validates:
  - `mcp_servers` + `mcp_tools` tables exist at head.
  - UNIQUE constraints present (team_id+name on servers; server_id+name on tools).
  - Round-trip: upgrade head → downgrade -1 → upgrade head succeeds (ADR-013 (e)).
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def test_mcp_tables_present_after_upgrade_head(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "mcp_servers" in result
    assert "mcp_tools" in result


async def test_mcp_servers_unique_constraint(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                WHERE cls.relname = 'mcp_servers'
                  AND con.contype = 'u'
                """
            )
        )
        names = {row[0] for row in rows}
    assert "uq_mcp_servers_team_name" in names


async def test_mcp_tools_unique_constraint(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                WHERE cls.relname = 'mcp_tools'
                  AND con.contype = 'u'
                """
            )
        )
        names = {row[0] for row in rows}
    assert "uq_mcp_tools_server_name" in names


def test_round_trip_downgrade_upgrade(alembic_config: Config) -> None:
    """upgrade head → downgrade -1 → upgrade head (ADR-013 (e))."""
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, "head")
