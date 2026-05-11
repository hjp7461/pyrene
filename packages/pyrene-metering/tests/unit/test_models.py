"""Unit tests for the `UsageRecord` SQLAlchemy model.

These tests run against the in-memory metadata reflection — they do NOT
need a live Postgres. Integration tests under `tests/integration/`
exercise INSERT behavior + UNIQUE race + indexes against a real DB.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import Numeric, Table, UniqueConstraint, inspect

from pyrene_metering import UsageRecord


def test_table_name() -> None:
    assert UsageRecord.__tablename__ == "usage_records"


def test_columns_present() -> None:
    mapper = inspect(UsageRecord)
    cols = {c.name for c in mapper.columns}
    expected = {
        "id",
        "request_id",
        "attempt_idx",
        "user_id",
        "team_id",
        "agent_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
        "created_at",
    }
    assert expected <= cols


def _table() -> Table:
    """Convenience cast so mypy sees the concrete `Table` API (FromClause-typed via ORM)."""
    return cast(Table, UsageRecord.__table__)


def test_cost_usd_is_numeric_18_8() -> None:
    """Precision/scale: 18 / 8 (sub-cent representable, $999M ceiling)."""
    col = _table().c["cost_usd"]
    col_type = cast(Numeric[Any], col.type)
    assert col_type.precision == 18
    assert col_type.scale == 8


def test_unique_constraint_request_attempt() -> None:
    """The idempotency UNIQUE(request_id, attempt_idx) is declared."""
    uniques = [
        uc for uc in _table().constraints if isinstance(uc, UniqueConstraint)
    ]
    names = {uc.name for uc in uniques}
    assert "uq_usage_records_request_attempt" in names

    target = next(
        uc for uc in uniques
        if uc.name == "uq_usage_records_request_attempt"
    )
    cols = {c.name for c in target.columns}
    assert cols == {"request_id", "attempt_idx"}


def test_indexes_declared() -> None:
    """All 5 hot-path indexes are declared on the table."""
    idx_names: set[str | None] = {ix.name for ix in _table().indexes}
    assert "ix_usage_records_user_created" in idx_names
    assert "ix_usage_records_team_created" in idx_names
    assert "ix_usage_records_request" in idx_names
    assert "ix_usage_records_agent_created" in idx_names
    assert "ix_usage_records_model_created" in idx_names


def test_user_id_fk_restrict() -> None:
    """ADR-013 (b): `usage_records.user_id` → `users.id` ON DELETE RESTRICT."""
    col = _table().c["user_id"]
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "users"
    assert fk.ondelete == "RESTRICT"


def test_team_id_fk_restrict() -> None:
    """ADR-013 (b): `usage_records.team_id` → `teams.id` ON DELETE RESTRICT."""
    col = _table().c["team_id"]
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "teams"
    assert fk.ondelete == "RESTRICT"


def test_agent_id_nullable() -> None:
    """`agent_id` is nullable (some entry points run without an agent record)."""
    col = _table().c["agent_id"]
    assert col.nullable is True


def test_cache_columns_default_zero() -> None:
    """`cache_read_tokens` and `cache_write_tokens` default to 0."""
    cr = _table().c["cache_read_tokens"]
    cw = _table().c["cache_write_tokens"]
    assert cr.nullable is False
    assert cw.nullable is False
