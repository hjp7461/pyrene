"""Unit tests for `pyrene_budget.aggregation` pure helpers.

DB-touching paths are covered in `tests/integration/`. Here we verify
the `_pct` rounding and the `_BudgetToMeteringPeriod` mapping.
"""

from __future__ import annotations

from decimal import Decimal

from pyrene_budget.aggregation import _BudgetToMeteringPeriod, _pct


def test_pct_zero_used() -> None:
    assert _pct(Decimal("0"), Decimal("5.00")) == Decimal("0.00")


def test_pct_half() -> None:
    assert _pct(Decimal("2.50"), Decimal("5.00")) == Decimal("50.00")


def test_pct_zero_limit_returns_zero() -> None:
    """Avoids ZeroDivisionError on unconfigured budgets."""
    assert _pct(Decimal("1"), Decimal("0")) == Decimal("0")


def test_pct_quantization() -> None:
    """Result is quantized to 2 decimals (Decimal — no float drift)."""
    out = _pct(Decimal("1"), Decimal("3"))
    # 33.33333... → 33.33 (ROUND_HALF_EVEN default).
    assert out == Decimal("33.33")


def test_period_mapping_identity() -> None:
    assert _BudgetToMeteringPeriod == {"day": "day", "week": "week", "month": "month"}
