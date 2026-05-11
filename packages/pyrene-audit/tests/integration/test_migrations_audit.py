"""Migration integration tests for 0006_audit_log.

Validates:
  - `audit_events` exists at TARGET_REVISION (0006).
  - WORM triggers + hash trigger exist.
  - Round-trip: upgrade target → downgrade -1 → upgrade target succeeds
    (ADR-013 (e)).
  - Indexes are in place (5 btree + 1 BRIN).
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def test_audit_events_table_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "audit_events" in tables


async def test_audit_worm_trigger_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT tgname FROM pg_trigger
                WHERE tgrelid = 'audit_events'::regclass
                  AND NOT tgisinternal
                """
            )
        )
        triggers = {r[0] for r in rows.all()}
    assert "audit_worm_trigger" in triggers
    assert "audit_hash_chain_trigger" in triggers


async def test_audit_indexes_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'audit_events'
                """
            )
        )
        idxs = {r[0] for r in rows.all()}
    expected = {
        "ix_audit_events_user_created",
        "ix_audit_events_team_chain_tip",
        "ix_audit_events_event_type_created",
        "ix_audit_events_request_id",
        "ix_audit_events_agent_created",
        "ix_audit_events_created_brin",
    }
    assert expected.issubset(idxs), f"missing indexes: {expected - idxs}"


async def test_brin_index_is_brin_type(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT am.amname FROM pg_class c
                JOIN pg_am am ON am.oid = c.relam
                WHERE c.relname = 'ix_audit_events_created_brin'
                """
            )
        )
        amname = rows.scalar_one()
    assert amname == "brin"


def test_round_trip_downgrade_upgrade(alembic_config: Config) -> None:
    """upgrade target → downgrade -1 → upgrade target (ADR-013 (e))."""
    # We are at TARGET_REVISION already (session fixture brought us up).
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, "0006_audit_log")
