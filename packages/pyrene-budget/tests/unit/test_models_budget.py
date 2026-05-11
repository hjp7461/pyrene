"""Unit tests for `BudgetLimit` ORM declarations.

We do not connect to a DB — these tests verify the SQLAlchemy mapping
and the table constraints declared on the `__table_args__`.
"""

from __future__ import annotations

from pyrene_budget.models import BudgetLimit, metadata


def test_table_name() -> None:
    assert BudgetLimit.__tablename__ == "budget_limits"


def test_unique_constraint_present() -> None:
    """`UNIQUE(scope, scope_id, period)` exists with the canonical name.

    The advisory-lock key derives from the same composite — the name
    being stable matters because operators look for it in `pg_indexes`.
    """
    table = metadata.tables["budget_limits"]
    uniques = [c for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"]
    matched = [u for u in uniques if u.name == "uq_budget_limits_scope_period"]
    assert len(matched) == 1
    cols = [c.name for c in matched[0].columns]  # type: ignore[attr-defined]
    assert cols == ["scope", "scope_id", "period"]


def test_scope_period_index_present() -> None:
    table = metadata.tables["budget_limits"]
    matched = [i for i in table.indexes if i.name == "ix_budget_limits_scope_period"]
    assert len(matched) == 1
    cols = [c.name for c in matched[0].columns]
    assert cols == ["scope", "scope_id", "period"]


def test_limit_usd_precision_decimal() -> None:
    """`limit_usd` is NUMERIC(18, 8) — matches `usage_records.cost_usd`."""
    table = metadata.tables["budget_limits"]
    col = table.columns["limit_usd"]
    # SQLAlchemy Numeric type carries .precision / .scale
    assert col.type.precision == 18  # type: ignore[attr-defined]
    assert col.type.scale == 8  # type: ignore[attr-defined]
