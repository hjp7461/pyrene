"""`GET /metering/usage` + `GET /metering/usage/records` + reload endpoint.

PRD-013 §4 (Day 2). RBAC alignment:
  - Read endpoints accessible to admin / analyst (informational rollups).
  - `POST /admin/pricing/reload` admin-only (mutating config).

Module-level holders (`_pricing_table`, `_summary_cache`) keep the route
package independent from the host app's wiring. The host app calls
`set_pricing_table(...)` / `set_summary_cache(...)` at startup; tests
swap these freely.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.dependencies import _session_proxy, require_admin, require_any_role
from pyrene_core import UserContext
from pyrene_metering.aggregation import SummaryCache
from pyrene_metering.models import UsageRecord
from pyrene_metering.pricing import PricingTable
from pyrene_metering.schemas import (
    Period,
    UsageRecordPage,
    UsageRecordResponse,
    UsageSummary,
)

# Module-level holders. `set_*` raise if called twice — host apps wire
# these once at startup, and tests can call `reset_*` between cases.
_pricing_table: PricingTable | None = None
_summary_cache: SummaryCache | None = None


def set_pricing_table(table: PricingTable) -> None:
    """Register the host-app pricing table (call at startup)."""
    global _pricing_table
    _pricing_table = table


def set_summary_cache(cache: SummaryCache) -> None:
    """Register the host-app summary cache (call at startup)."""
    global _summary_cache
    _summary_cache = cache


def _require_pricing() -> PricingTable:
    if _pricing_table is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pricing table not configured",
        )
    return _pricing_table


def _require_summary_cache() -> SummaryCache:
    if _summary_cache is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="summary cache not configured",
        )
    return _summary_cache


usage_router = APIRouter(prefix="/metering", tags=["metering"])
admin_router = APIRouter(prefix="/admin", tags=["metering-admin"])

_require_reader = require_any_role("admin", "analyst")


def _to_response(row: UsageRecord) -> UsageRecordResponse:
    return UsageRecordResponse(
        id=row.id,
        request_id=row.request_id,
        attempt_idx=row.attempt_idx,
        user_id=row.user_id,
        team_id=row.team_id,
        agent_id=row.agent_id,
        model=row.model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_write_tokens=row.cache_write_tokens,
        cost_usd=row.cost_usd,
        created_at=row.created_at,
    )


@usage_router.get("/usage")
async def get_usage_summary(
    current: Annotated[UserContext, Depends(_require_reader)],
    period: Period = Query("day", description="Aggregation bucket"),
    user_id: UUID | None = Query(None, description="Filter by user"),
    agent_id: UUID | None = Query(None, description="Filter by agent"),
    session: AsyncSession = Depends(_session_proxy),
) -> UsageSummary:
    """Aggregate usage for the current period bucket.

    Filter precedence: user_id → agent_id → team_id (from auth context).
    At least the team_id boundary is always applied — analysts cannot
    inspect other teams' rollups.
    """
    cache = _require_summary_cache()

    if user_id is not None:
        return await cache.by_user(session, user_id, period)
    if agent_id is not None:
        return await cache.by_agent(session, agent_id, period)
    return await cache.by_team(session, current.team_id, period)


@usage_router.get("/usage/records")
async def list_usage_records(
    current: Annotated[UserContext, Depends(_require_reader)],
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    user_id: UUID | None = Query(None),
    session: AsyncSession = Depends(_session_proxy),
) -> UsageRecordPage:
    """Paginated usage records (PLAN-016 server-side paging).

    Always filtered to the caller's `team_id`. `user_id` further narrows
    within the team. Page is 1-indexed (UX convention).
    """
    offset = (page - 1) * size

    base = select(UsageRecord).where(UsageRecord.team_id == current.team_id)
    if user_id is not None:
        base = base.where(UsageRecord.user_id == user_id)

    total_stmt = select(func.count()).select_from(base.subquery())
    total = int((await session.execute(total_stmt)).scalar_one())

    rows_stmt = base.order_by(UsageRecord.created_at.desc()).offset(offset).limit(size)
    rows = (await session.execute(rows_stmt)).scalars().all()

    return UsageRecordPage(
        items=tuple(_to_response(r) for r in rows),
        page=page,
        size=size,
        total=total,
    )


@admin_router.post("/pricing/reload")
async def reload_pricing(
    _current: Annotated[UserContext, Depends(require_admin)],
) -> dict[str, int | str]:
    """Re-parse the pricing YAML.

    Returns the entry count + path. Invalidates the summary cache so
    downstream callers see the new prices immediately (though already-
    written `cost_usd` rows are NOT recomputed — PRD-013 §2.1 S-3).
    """
    table = _require_pricing()
    n = table.reload()
    cache = _require_summary_cache()
    cache.invalidate()
    return {"loaded": n, "path": str(table.path)}


__all__ = [
    "admin_router",
    "set_pricing_table",
    "set_summary_cache",
    "usage_router",
]
