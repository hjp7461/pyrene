"""Integration tests for `0004_rbac_matrix` migration.

Validates:
  - `permissions` table exists at the PLAN-010 revision.
  - UNIQUE constraint `(role_id, tool_name, action)` is present.
  - `(tool_name, role_id)` composite index is created.
  - FK on `role_id` enforces RESTRICT (ADR-013 (b)).
  - Round-trip upgrade → downgrade → upgrade succeeds.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

TARGET_REVISION = "0004_rbac_matrix"


async def test_permissions_table_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "permissions" in result


async def test_permissions_unique_constraint(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                WHERE cls.relname = 'permissions'
                  AND con.contype = 'u'
                """
            )
        )
        names = {row[0] for row in rows}
    assert "uq_permissions_role_tool_action" in names


async def test_permissions_composite_index(engine: AsyncEngine) -> None:
    """The (tool_name, role_id) index drives the RBAC WHERE clause."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE tablename = 'permissions'
                """
            )
        )
        names = {row[0] for row in rows}
    assert "ix_permissions_tool_role" in names


async def test_permissions_role_id_fk_restrict(engine: AsyncEngine) -> None:
    """ADR-013 (b) — role_id FK is RESTRICT, not CASCADE/SET NULL."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT confdeltype
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                WHERE cls.relname = 'permissions'
                  AND con.conname = 'fk_permissions_role_id'
                """
            )
        )
        # asyncpg returns `confdeltype` (pg "char") as bytes; normalize.
        types = {
            (row[0].decode() if isinstance(row[0], bytes) else row[0])
            for row in rows
        }
    # `r` = RESTRICT, `c` = CASCADE, `n` = SET NULL (pg_constraint).
    assert types == {"r"}


def test_round_trip_downgrade_upgrade(alembic_config: Config) -> None:
    """upgrade 0004 → downgrade -1 → upgrade 0004 succeeds (ADR-013 (e))."""
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, TARGET_REVISION)
