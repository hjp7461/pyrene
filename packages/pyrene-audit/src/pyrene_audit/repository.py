"""Data-access functions for `audit_events`.

PLAN-015 Day 2. Keeps the route layer free of inline `select(...)`
boilerplate and gives unit tests a seam to inject a session fake.

All queries filter on `team_id` first because the WORM table is
multi-tenant and an admin in team A must not see team B's events. The
route layer pulls `current.team_id` from `UserContext` and forwards it
here.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_audit.models import AuditEventRow


def _base_query(team_id: UUID) -> Select[tuple[AuditEventRow]]:
    """Team-scoped base query — every other helper layers filters on top."""
    return select(AuditEventRow).where(AuditEventRow.team_id == team_id)


async def list_events(
    session: AsyncSession,
    *,
    team_id: UUID,
    user_id: UUID | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[AuditEventRow], int]:
    """Paginated event list within a team.

    Returns `(items, total)` — total count is the unfiltered-by-page
    matching set, used by the API for client-side pagination.
    """
    q = _base_query(team_id)
    if user_id is not None:
        q = q.where(AuditEventRow.user_id == user_id)
    if event_type is not None:
        q = q.where(AuditEventRow.event_type == event_type)
    if since is not None:
        q = q.where(AuditEventRow.created_at >= since)
    if until is not None:
        q = q.where(AuditEventRow.created_at <= until)

    count_q = select(func.count()).select_from(q.subquery())
    total_result = await session.execute(count_q)
    total = int(total_result.scalar_one())

    offset = (page - 1) * size
    page_q = (
        q.order_by(AuditEventRow.created_at.desc(), AuditEventRow.id.desc())
        .limit(size)
        .offset(offset)
    )
    rows_result = await session.execute(page_q)
    items = list(rows_result.scalars().all())
    return items, total


async def timeline_buckets(
    session: AsyncSession,
    *,
    team_id: UUID,
    since: datetime,
    until: datetime,
) -> list[tuple[datetime, int]]:
    """Hour-bucketed event counts for the dashboard timeline.

    Postgres `date_trunc('hour', ...)` lets BRIN on `created_at` still
    help with the range scan.
    """
    bucket = func.date_trunc("hour", AuditEventRow.created_at).label("bucket")
    # `count` clashes with `Row.count` (the method); use `n` to dodge the
    # mypy false positive on the SQLAlchemy Row reflection type.
    q = (
        select(bucket, func.count().label("n"))
        .where(AuditEventRow.team_id == team_id)
        .where(AuditEventRow.created_at >= since)
        .where(AuditEventRow.created_at <= until)
        .group_by(bucket)
        .order_by(bucket)
    )
    result = await session.execute(q)
    return [(row[0], int(row[1])) for row in result.all()]


__all__ = ["list_events", "timeline_buckets"]
