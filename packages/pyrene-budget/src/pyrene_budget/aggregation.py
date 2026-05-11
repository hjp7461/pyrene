"""Used / remaining budget aggregation.

PLAN-014 Day 2. The pre-flight + GET-status endpoints both need a
single async helper that:
  1. Reads `BudgetLimit` for (scope, scope_id, period).
  2. Reads the matching `UsageSummary` from metering (PRD-013
     handoff — `pyrene_metering.usage_by_user / usage_by_team`).
  3. Returns a fully-populated `BudgetStatus` (limit / used / remaining
     / pct).

### Period mapping

Both packages use the same closed set `{"day", "week", "month"}` and the
same UTC bucket math (`pyrene_metering.aggregation._period_start`).
This is *not* by coincidence: PRD-014 §2.1 specifies daily/monthly and
PRD-013 already supplies day/week/month aggregations. Reusing the
metering helper keeps period semantics single-sourced.

### Cache integration

The pre-flight hook is invoked once per request — at most a few times
per second per (scope_id, period). The metering `SummaryCache` already
memoizes for 60s, which is more than enough; we pass the same cache
instance from the host wiring so budget reads benefit from the existing
TTL.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_budget.errors import BudgetSystemUnavailableError
from pyrene_budget.repository import get_budget_limit
from pyrene_budget.schemas import BudgetPeriod, BudgetScope, BudgetStatus
from pyrene_metering.aggregation import SummaryCache, usage_by_team, usage_by_user
from pyrene_metering.schemas import Period, UsageSummary

# PRD-014 ↔ PRD-013 period bridging. Both use the same set today; the
# alias documents the contract.
_BudgetToMeteringPeriod: dict[BudgetPeriod, Period] = {
    "day": "day",
    "week": "week",
    "month": "month",
}


async def _used_usd(
    session: AsyncSession,
    *,
    scope: BudgetScope,
    scope_id: UUID,
    period: BudgetPeriod,
    cache: SummaryCache | None,
) -> UsageSummary:
    """Pull the matching `UsageSummary` (used cost in current bucket).

    Uses the cache when supplied (production hot path). Tests can pass
    `cache=None` to force a fresh aggregation.
    """
    metering_period = _BudgetToMeteringPeriod[period]
    if cache is not None:
        if scope == "user":
            return await cache.by_user(session, scope_id, metering_period)
        return await cache.by_team(session, scope_id, metering_period)
    if scope == "user":
        return await usage_by_user(session, scope_id, metering_period)
    return await usage_by_team(session, scope_id, metering_period)


def _pct(used: Decimal, limit: Decimal) -> Decimal:
    """0..100 inclusive. Defined as 0 for `limit == 0` (no budget set)."""
    if limit <= Decimal("0"):
        return Decimal("0")
    return (used / limit * Decimal("100")).quantize(Decimal("0.01"))


async def remaining_budget(
    session: AsyncSession,
    *,
    scope: BudgetScope,
    scope_id: UUID,
    period: BudgetPeriod,
    cache: SummaryCache | None = None,
) -> Decimal:
    """Return remaining $ for (scope, scope_id, period).

    Raises `BudgetSystemUnavailableError` if no `BudgetLimit` is configured —
    callers (admin endpoint, dashboard) decide whether to treat absence
    as "unlimited" or "fail-closed". The pre-flight hook (where the
    decision matters) catches this and falls through to the global
    default. The shape kept here keeps the helper unambiguous.
    """
    row = await get_budget_limit(
        session, scope=scope, scope_id=scope_id, period=period
    )
    if row is None:
        raise BudgetSystemUnavailableError(
            f"no budget configured for scope={scope} scope_id={scope_id} "
            f"period={period}"
        )
    summary = await _used_usd(
        session, scope=scope, scope_id=scope_id, period=period, cache=cache
    )
    return Decimal(row.limit_usd) - Decimal(summary.total_cost_usd)


async def status_for(
    session: AsyncSession,
    *,
    scope: BudgetScope,
    scope_id: UUID,
    period: BudgetPeriod,
    cache: SummaryCache | None = None,
) -> BudgetStatus | None:
    """Full `BudgetStatus` for the dashboard endpoint.

    Returns None when no limit is set (the route layer maps to 404).
    The aggregation hits at most two queries (limit lookup + cached
    summary).
    """
    row = await get_budget_limit(
        session, scope=scope, scope_id=scope_id, period=period
    )
    if row is None:
        return None
    summary = await _used_usd(
        session, scope=scope, scope_id=scope_id, period=period, cache=cache
    )
    used = Decimal(summary.total_cost_usd)
    limit_usd = Decimal(row.limit_usd)
    return BudgetStatus(
        scope=scope,
        scope_id=scope_id,
        period=period,
        period_label=summary.period_label,
        limit_usd=limit_usd,
        used_usd=used,
        remaining_usd=limit_usd - used,
        used_pct=_pct(used, limit_usd),
    )


__all__ = [
    "remaining_budget",
    "status_for",
]
