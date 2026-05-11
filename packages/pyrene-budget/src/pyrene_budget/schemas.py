"""Pydantic DTOs for the budget API.

Read + write shapes for `/budgets`. Mirrors the metering `schemas.py`
pattern: `StrictBaseModel` everywhere, `Decimal` preserved.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pyrene_core import StrictBaseModel

# Closed sets. Adding values is a schema migration, not a free-text
# proliferation (mirrors PRD-014 §4 / pyrene-core audit `Literal`s).
BudgetScope = Literal["user", "team"]
BudgetPeriod = Literal["day", "week", "month"]


class BudgetLimitCreate(StrictBaseModel):
    """POST `/budgets` body."""

    scope: BudgetScope
    scope_id: UUID
    period: BudgetPeriod
    limit_usd: Decimal


class BudgetLimitResponse(StrictBaseModel):
    """GET `/budgets` element + POST response."""

    id: UUID
    scope: BudgetScope
    scope_id: UUID
    period: BudgetPeriod
    limit_usd: Decimal
    created_at: datetime
    updated_at: datetime


class BudgetStatus(StrictBaseModel):
    """Aggregated read for the dashboard / pre-flight gate.

    `used_usd` is summed from `usage_records` for the current period
    bucket; `remaining_usd = limit_usd - used_usd` (Decimal subtraction
    is exact at NUMERIC(18, 8)).
    """

    scope: BudgetScope
    scope_id: UUID
    period: BudgetPeriod
    period_label: str  # ISO label (mirrors metering UsageSummary.period_label).
    limit_usd: Decimal
    used_usd: Decimal
    remaining_usd: Decimal
    used_pct: Decimal  # 0..100, two decimal precision (Decimal not float).


__all__ = [
    "BudgetLimitCreate",
    "BudgetLimitResponse",
    "BudgetPeriod",
    "BudgetScope",
    "BudgetStatus",
]
