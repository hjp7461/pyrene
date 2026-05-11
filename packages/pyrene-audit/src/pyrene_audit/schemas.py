"""Pydantic response schemas for the audit query API.

PLAN-015 Day 2. The DB row carries `event_metadata` (because `metadata`
is a reserved attribute on SQLAlchemy declarative models); the response
exposes it as `metadata` to match PRD-015 §4's wire shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from pyrene_core.models import StrictBaseModel


class AuditEventResponse(StrictBaseModel):
    """One audit row, wire-format.

    `prev_hash` / `row_hash` are bytea on the DB side; we expose them as
    hex strings (or null for the first row's `prev_hash`). Auditors can
    re-compute the chain externally with `bytes.fromhex(...)`.
    """

    id: UUID
    event_type: str
    user_id: UUID | None
    team_id: UUID | None
    agent_id: UUID | None
    request_id: UUID | None
    tool_name: str | None
    outcome: str
    metadata: dict[str, Any]
    prev_hash: str | None = None
    row_hash: str
    created_at: datetime


class AuditTimelineBucket(StrictBaseModel):
    """One hour-bucket of audit counts (PRD-016 timeline view source)."""

    bucket: datetime
    count: int


class AuditEventPage(StrictBaseModel):
    """Paginated audit query result."""

    items: list[AuditEventResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    size: int = Field(ge=1, le=500)


__all__ = ["AuditEventPage", "AuditEventResponse", "AuditTimelineBucket"]
