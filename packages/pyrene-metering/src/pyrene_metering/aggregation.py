"""SQL aggregation: usage rollups by user / agent / model.

PRD-013 §4 + Day 2. Two parallel signatures (`usage_by_user`,
`usage_by_agent`) so callers do not have to constrain by both axes
simultaneously.

### Decimal arithmetic

Postgres `SUM(NUMERIC)` returns `Decimal` through asyncpg, which the
ORM preserves. The Pydantic `UsageSummary` accepts `Decimal` natively,
so the precision contract from `cost_usd: Numeric(18, 8)` survives to
the JSON payload.

### Period truncation

`day`/`week`/`month` map to Postgres `DATE_TRUNC` buckets. The summary
returns the lower bound of the bucket as an ISO label (`2026-05` for
`month`, `2026-W19` for `week`, `2026-05-11` for `day`).

### TTL cache

`SummaryCache` wraps `cachetools.TTLCache` (60s default). The cache key
is `(scope, scope_id, period, now-bucket)`; the bucket discretization
ensures the cache is naturally invalidated at period boundaries.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from cachetools import TTLCache
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_metering.models import UsageRecord
from pyrene_metering.schemas import Period, UsageSummary

# Internal scope tag for cache keys — keeps user_id and agent_id in
# disjoint namespaces (so an agent_id colliding with a user_id can't
# alias).
_Scope = Literal["user", "agent", "team"]


def _period_label(period: Period, when: datetime) -> str:
    """ISO-flavored label for the truncated bucket."""
    if period == "day":
        return when.strftime("%Y-%m-%d")
    if period == "week":
        # ISO week: %G-W%V (Python 3.13's %G is the ISO 8601 year).
        return when.strftime("%G-W%V")
    return when.strftime("%Y-%m")


def _now() -> datetime:
    return datetime.now(UTC)


def _period_start(period: Period, when: datetime) -> datetime:
    """Lower bound of the current bucket (rounded down)."""
    if period == "day":
        return when.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        iso_year, iso_week, _ = when.isocalendar()
        # Monday of the ISO week.
        return datetime.fromisocalendar(iso_year, iso_week, 1).replace(tzinfo=UTC)
    # month
    return when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _aggregate(
    session: AsyncSession,
    *,
    period: Period,
    user_id: UUID | None = None,
    agent_id: UUID | None = None,
    team_id: UUID | None = None,
) -> UsageSummary:
    """Run the GROUP BY + SUM for the current period bucket.

    Exactly one of `user_id` / `agent_id` / `team_id` may be set (or
    none — global rollup). Mixing two filters is allowed: e.g. agent_id +
    team_id narrows to a team's specific agent.
    """
    bucket_start = _period_start(period, _now())

    stmt = select(
        func.coalesce(func.sum(UsageRecord.input_tokens), 0),
        func.coalesce(func.sum(UsageRecord.output_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cache_write_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost_usd), Decimal("0")),
        func.count(UsageRecord.id),
        func.count(func.distinct(UsageRecord.request_id)),
    ).where(UsageRecord.created_at >= bucket_start)

    if user_id is not None:
        stmt = stmt.where(UsageRecord.user_id == user_id)
    if agent_id is not None:
        stmt = stmt.where(UsageRecord.agent_id == agent_id)
    if team_id is not None:
        stmt = stmt.where(UsageRecord.team_id == team_id)

    row = (await session.execute(stmt)).one()
    (
        total_in,
        total_out,
        total_cr,
        total_cw,
        total_cost,
        row_count,
        request_count,
    ) = row

    # avg_attempts: total rows / distinct request_id. 0 when there is no data.
    if request_count and int(request_count) > 0:
        avg = Decimal(int(row_count)) / Decimal(int(request_count))
    else:
        avg = Decimal("0")

    return UsageSummary(
        period=period,
        period_label=_period_label(period, bucket_start),
        total_input_tokens=int(total_in),
        total_output_tokens=int(total_out),
        total_cache_read_tokens=int(total_cr),
        total_cache_write_tokens=int(total_cw),
        total_cost_usd=Decimal(total_cost),
        request_count=int(request_count),
        avg_attempts=avg,
    )


async def usage_by_user(
    session: AsyncSession, user_id: UUID, period: Period
) -> UsageSummary:
    """Aggregate usage for a single user in the current `period` bucket."""
    return await _aggregate(session, period=period, user_id=user_id)


async def usage_by_agent(
    session: AsyncSession, agent_id: UUID, period: Period
) -> UsageSummary:
    """Aggregate usage for a single agent in the current `period` bucket."""
    return await _aggregate(session, period=period, agent_id=agent_id)


async def usage_by_team(
    session: AsyncSession, team_id: UUID, period: Period
) -> UsageSummary:
    """Aggregate usage for a single team in the current `period` bucket.

    Provided alongside the by-user / by-agent paths because PLAN-014
    (budget) operates at the team boundary.
    """
    return await _aggregate(session, period=period, team_id=team_id)


class SummaryCache:
    """TTL-based memoization for `usage_by_*` results.

    Cache keys are `(scope, scope_id, period, bucket_start_epoch)`. The
    bucket epoch ensures that crossing a period boundary forces a fresh
    aggregation (the key naturally changes). TTL (60s) covers within-bucket
    rapid polling from PLAN-016 dashboards / PLAN-014 budget gates.
    """

    def __init__(self, *, ttl_seconds: int = 60, maxsize: int = 1024) -> None:
        self._cache: TTLCache[tuple[_Scope, str, Period, int], UsageSummary] = (
            TTLCache(maxsize=maxsize, ttl=ttl_seconds)
        )
        self._ttl = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def _key(
        self, scope: _Scope, scope_id: UUID, period: Period
    ) -> tuple[_Scope, str, Period, int]:
        bucket_epoch = int(_period_start(period, _now()).timestamp())
        return (scope, str(scope_id), period, bucket_epoch)

    def invalidate(self) -> None:
        self._cache.clear()

    async def by_user(
        self, session: AsyncSession, user_id: UUID, period: Period
    ) -> UsageSummary:
        key = self._key("user", user_id, period)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        fresh = await usage_by_user(session, user_id, period)
        self._cache[key] = fresh
        return fresh

    async def by_agent(
        self, session: AsyncSession, agent_id: UUID, period: Period
    ) -> UsageSummary:
        key = self._key("agent", agent_id, period)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        fresh = await usage_by_agent(session, agent_id, period)
        self._cache[key] = fresh
        return fresh

    async def by_team(
        self, session: AsyncSession, team_id: UUID, period: Period
    ) -> UsageSummary:
        key = self._key("team", team_id, period)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        fresh = await usage_by_team(session, team_id, period)
        self._cache[key] = fresh
        return fresh


# Test seam: monotonic clock for cache TTL behavior verification.
def _monotonic() -> float:
    return time.monotonic()


__all__ = [
    "SummaryCache",
    "usage_by_agent",
    "usage_by_team",
    "usage_by_user",
]
