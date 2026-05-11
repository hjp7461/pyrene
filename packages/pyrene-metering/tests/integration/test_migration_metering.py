"""Round-trip migration test for 0005_cost_metering.

Validates:
  - `usage_records` table + indexes + UNIQUE constraint present at head.
  - `cost_usd` column is NUMERIC(18, 8) (sub-cent precision contract).
  - Round-trip: upgrade head → downgrade -1 → upgrade head succeeds
    (ADR-013 (e)).
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def test_usage_records_table_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "usage_records" in tables


async def test_unique_constraint_request_attempt(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                WHERE cls.relname = 'usage_records'
                  AND con.contype = 'u'
                """
            )
        )
        names = {row[0] for row in rows}
    assert "uq_usage_records_request_attempt" in names


async def test_cost_usd_numeric_precision(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_name = 'usage_records' AND column_name = 'cost_usd'
                """
            )
        )
        row = rows.one()
    assert row.numeric_precision == 18
    assert row.numeric_scale == 8


async def test_indexes_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'usage_records'"
            )
        )
        names = {row[0] for row in rows}
    assert "ix_usage_records_user_created" in names
    assert "ix_usage_records_team_created" in names
    assert "ix_usage_records_request" in names
    assert "ix_usage_records_agent_created" in names
    assert "ix_usage_records_model_created" in names


def _normalize_char(val: object) -> str:
    """asyncpg returns `char(1)` columns as `bytes` (b'r'); decode for compare."""
    if isinstance(val, bytes):
        return val.decode("ascii")
    return str(val)


async def test_fk_user_id_restrict(engine: AsyncEngine) -> None:
    """ON DELETE RESTRICT on `user_id` FK (ADR-013 (b))."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT confdeltype
                FROM pg_constraint
                WHERE conname = 'fk_usage_records_user_id'
                """
            )
        )
        delete_type = rows.scalar_one()
    # Postgres encodes RESTRICT as 'r' (returned as bytes by asyncpg).
    assert _normalize_char(delete_type) == "r"


async def test_fk_team_id_restrict(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT confdeltype
                FROM pg_constraint
                WHERE conname = 'fk_usage_records_team_id'
                """
            )
        )
        delete_type = rows.scalar_one()
    assert _normalize_char(delete_type) == "r"


def test_round_trip(alembic_config: Config) -> None:
    """ADR-013 (e): upgrade head → downgrade -1 → upgrade head."""
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, "0005_cost_metering")
