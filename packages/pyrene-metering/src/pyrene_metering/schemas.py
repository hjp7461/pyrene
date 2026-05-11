"""Pydantic DTOs for the metering API.

Read-only DTOs (`UsageRecordResponse`, `UsageSummary`) follow the
`StrictBaseModel` pattern shared across packages. `Decimal` is preserved
through to JSON serialization (Pydantic emits as string under
`model_config["json_encoders"]` default for Decimal — preserving precision).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pyrene_core import StrictBaseModel

# Aggregation period: "day" → DATE_TRUNC('day'), "week" → ('week'),
# "month" → ('month'). The summary `period_label` echoes the truncation
# boundary in ISO form.
Period = Literal["day", "week", "month"]


class UsageRecordResponse(StrictBaseModel):
    """One usage row, serialized for the API.

    `cost_usd` is `Decimal`; Pydantic serializes Decimal as JSON string
    by default which preserves the 8 decimal places.
    """

    id: UUID
    request_id: UUID
    attempt_idx: int
    user_id: UUID
    team_id: UUID
    agent_id: UUID | None
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal
    created_at: datetime


class UsageSummary(StrictBaseModel):
    """Aggregated usage over a period.

    Used by Day 2 `GET /metering/usage` and (Phase 2) PLAN-014 budget
    polling. The shape is the canonical handoff between metering and
    downstream consumers — adding fields requires a coordinated PLAN
    amendment.
    """

    period: Period
    period_label: str  # e.g. "2026-05" (month) / "2026-W19" (week) / "2026-05-11" (day)
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_write_tokens: int
    total_cost_usd: Decimal
    request_count: int
    avg_attempts: Decimal  # Average attempts per unique request_id (PRD-013 retry signal).


class UsageRecordPage(StrictBaseModel):
    """Server-side paginated page of usage records (PLAN-016)."""

    items: tuple[UsageRecordResponse, ...]
    page: int
    size: int
    total: int


__all__ = [
    "Period",
    "UsageRecordPage",
    "UsageRecordResponse",
    "UsageSummary",
]
