"""`GET /audit/events` and `/audit/timeline` — admin-only audit queries.

PLAN-015 Day 2. Reads are admin-only because audit rows are sensitive
(they encode who tried what + which tools fired). The query is always
team-scoped — even an admin sees only their own team's audit log. A
"super admin" cross-tenant view is out of scope for Phase 2.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_audit.models import AuditEventRow
from pyrene_audit.repository import list_events, timeline_buckets
from pyrene_audit.schemas import (
    AuditEventPage,
    AuditEventResponse,
    AuditTimelineBucket,
)
from pyrene_auth.dependencies import _session_proxy, require_admin
from pyrene_core import UserContext

audit_router = APIRouter(prefix="/audit", tags=["audit"])


def _to_response(row: AuditEventRow) -> AuditEventResponse:
    return AuditEventResponse(
        id=row.id,
        event_type=row.event_type,
        user_id=row.user_id,
        team_id=row.team_id,
        agent_id=row.agent_id,
        request_id=row.request_id,
        tool_name=row.tool_name,
        outcome=row.outcome,
        metadata=dict(row.event_metadata),
        prev_hash=row.prev_hash.hex() if row.prev_hash is not None else None,
        row_hash=row.row_hash.hex(),
        created_at=row.created_at,
    )


@audit_router.get("/events")
async def list_events_endpoint(
    current: Annotated[UserContext, Depends(require_admin)],
    user_id: UUID | None = Query(default=None),
    event_type: str | None = Query(default=None, max_length=64),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(_session_proxy),
) -> AuditEventPage:
    items, total = await list_events(
        session,
        team_id=current.team_id,
        user_id=user_id,
        event_type=event_type,
        since=since,
        until=until,
        page=page,
        size=size,
    )
    return AuditEventPage(
        items=[_to_response(r) for r in items],
        total=total,
        page=page,
        size=size,
    )


@audit_router.get("/timeline")
async def timeline_endpoint(
    current: Annotated[UserContext, Depends(require_admin)],
    since: datetime = Query(...),
    until: datetime = Query(...),
    session: AsyncSession = Depends(_session_proxy),
) -> list[AuditTimelineBucket]:
    buckets = await timeline_buckets(
        session,
        team_id=current.team_id,
        since=since,
        until=until,
    )
    return [AuditTimelineBucket(bucket=b, count=c) for b, c in buckets]


__all__ = ["audit_router"]
