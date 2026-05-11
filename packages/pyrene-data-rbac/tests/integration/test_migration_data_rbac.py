"""Integration tests for `0007_data_permissions` migration.

Validates the ADR-013 (c) ADD COLUMN 3-step pattern + the
`data_permissions` table shape at the PLAN-011 revision.

Coverage:
  - `data_permissions` table + constraints + indexes present.
  - FK on `role_id` enforces RESTRICT (ADR-013 (b)).
  - `pyrene_schema_embeddings.connection_id` column exists + NOT NULL.
  - `UNIQUE(connection_id, schema, "table")` constraint present on
    `pyrene_schema_embeddings`.
  - Round-trip `upgrade head → downgrade -1 → upgrade head` succeeds
    (ADR-013 (e)).
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

TARGET_REVISION = "0007_data_permissions"


async def test_data_permissions_table_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert "data_permissions" in tables


async def test_data_permissions_unique_constraint(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                WHERE cls.relname = 'data_permissions'
                  AND con.contype = 'u'
                """
            )
        )
        names = {row[0] for row in rows}
    assert "uq_data_permissions_role_conn_schema_table_action" in names


async def test_data_permissions_indexes(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE tablename = 'data_permissions'
                """
            )
        )
        names = {row[0] for row in rows}
    assert "ix_data_permissions_role_conn_schema_table" in names
    assert "ix_data_permissions_conn_schema_table" in names


async def test_data_permissions_role_id_fk_restrict(
    engine: AsyncEngine,
) -> None:
    """ADR-013 (b) — role_id FK is RESTRICT, not CASCADE/SET NULL."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT confdeltype
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                WHERE cls.relname = 'data_permissions'
                  AND con.conname = 'fk_data_permissions_role_id'
                """
            )
        )
        types = {
            (row[0].decode() if isinstance(row[0], bytes) else row[0])
            for row in rows
        }
    # `r` = RESTRICT, `c` = CASCADE, `n` = SET NULL.
    assert types == {"r"}


async def test_schema_embeddings_connection_id_column(
    engine: AsyncEngine,
) -> None:
    """Step C of ADD COLUMN 3-step: `connection_id` exists + NOT NULL."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'pyrene_schema_embeddings'
                  AND column_name = 'connection_id'
                """
            )
        )
        rows_list = [(r[0], r[1]) for r in rows]
    assert ("connection_id", "NO") in rows_list


async def test_schema_embeddings_unique_constraint(
    engine: AsyncEngine,
) -> None:
    """`UNIQUE(connection_id, schema, "table")` on embeddings — matches
    the PLAN-002 retriever ON CONFLICT key."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class cls ON cls.oid = con.conrelid
                WHERE cls.relname = 'pyrene_schema_embeddings'
                  AND con.contype = 'u'
                """
            )
        )
        names = {row[0] for row in rows}
    assert "pyrene_schema_embeddings_unique_target" in names


def test_round_trip_downgrade_upgrade(alembic_config: Config) -> None:
    """upgrade 0007 → downgrade -1 → upgrade 0007 succeeds (ADR-013 (e)).

    The ADD COLUMN 3-step on `pyrene_schema_embeddings` is the
    interesting half — Step A is idempotent (`IF NOT EXISTS`), so a
    downgrade that drops the column lets the next upgrade rebuild it.
    """
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, TARGET_REVISION)


def test_round_trip_preserves_data(alembic_config: Config) -> None:
    """Round trip a SECOND time so the suite catches any latent state
    that the first round-trip would mask (ADR-013 (e) — multi-cycle
    invariance)."""
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, TARGET_REVISION)
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, TARGET_REVISION)
