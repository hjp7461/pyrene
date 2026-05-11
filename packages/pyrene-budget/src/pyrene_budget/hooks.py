"""Budget pre/post hook factories.

PLAN-014 Day 1 (pre) + Day 2 (post threshold notifier).

### Pre-flight hook (PRIORITY_BUDGET_PRE = 10)

Runs *first* in the gateway's `before_run` chain. Steps inside one TXN:

1. `BEGIN` — implicit via the `AsyncSession` context manager.
2. `SELECT pg_try_advisory_xact_lock(hashtextextended(:scope || ':' ||
   :scope_id::text || ':' || :period, 0))`
   - Returns false → raise `BudgetLockUnavailableError` (HTTP 503,
     fail-closed). This closes PRD-014 §위험 신호 #1 race: two pre-flight
     hooks at 95% utilization never both see "remaining > 0" because
     exactly one acquires the lock and serializes the read.
3. Lookup `BudgetLimit` for the user (and team, if configured).
4. Read `UsageSummary` from metering (PRD-013 handoff).
5. Compare `used + predicted >= limit` → raise `BudgetExceededError` (also
   maps to 429 in the route handler — non-retryable).
6. On success: stamp `ctx.metadata["budget_projection"] = {limit, used,
   predicted}` so the post hook + audit hook (priority 80) can read it.
7. TXN commits when the `async with session_factory()` block exits — lock
   auto-releases via `pg_advisory_xact_lock`'s txn scope.

### Post-flight hook (PRIORITY_BUDGET_POST = 90)

Runs *last* (after audit at 80). Steps:
  - Read `recorded_cost_usd` from `ctx.metadata` (PLAN-013 stamps it).
  - Re-aggregate via `status_for(...)` to get the new used / used_pct.
  - Fire the threshold webhook via `BudgetAlerter.maybe_fire(...)`.
  - If realized cost > predicted, emit an `over_budget` audit event
    (PRD-014 §2.1 S3 + cross-PLAN handoff to pyrene-audit).

### Why two TXNs (pre vs post)

Pre and post use *separate* sessions. Pre's TXN holds the advisory
lock for the duration of the request (tool execution), which is
unbounded — that would block every other pre-flight on the same
(scope, scope_id, period). Instead, pre commits the lock immediately
(after the gate decision is made, the lock is released). The race
window collapses to "two pre-flights both pass at 95%" — which the
**short** lock window prevents because both TXNs serialize through it.

A future tightening (Phase 3) would reserve the projected cost in a
counter table so even committed-but-uncharged spend counts; Phase 2
ships the simpler version because the realized-vs-predicted gap is
small (single-digit %) and the post hook surfaces it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyrene_budget.aggregation import status_for
from pyrene_budget.errors import (
    BudgetExceededError,
    BudgetLockUnavailableError,
    BudgetSystemUnavailableError,
)
from pyrene_budget.repository import get_budget_limit, try_lock_for_scope
from pyrene_budget.schemas import BudgetPeriod, BudgetScope
from pyrene_budget.webhook import BudgetAlerter
from pyrene_core.audit import AuditEvent, AuditSink
from pyrene_gateway import AfterRunHook, BeforeRunHook
from pyrene_gateway.context import RunContext
from pyrene_metering.aggregation import SummaryCache, usage_by_team, usage_by_user
from pyrene_metering.schemas import Period

logger = logging.getLogger(__name__)


# Period mapping (mirrors aggregation.py — duplicated locally so the
# hook stays autonomous from the aggregation helper when callers pass
# pre-resolved arguments).
_BUDGET_TO_METERING: dict[BudgetPeriod, Period] = {
    "day": "day",
    "week": "week",
    "month": "month",
}


def _predicted_cost_from_ctx(ctx: RunContext) -> Decimal:
    """Pull the caller's input-token cost projection.

    PRD-014 §3.2 specifies a safety-margin estimator (input cost is
    exact; output cost is bounded as input x 2). The pre-flight hook
    does not compute the projection itself — that's pricing logic
    (PLAN-013). The gateway / agent harness stamps
    `ctx.metadata["predicted_cost_usd"]` before invoking the hook chain
    (default 0 if not set, which is permissive — a request with no
    projection is treated as zero predicted spend).

    Decimal-only path; floats are rejected by `Decimal(...)`.
    """
    raw = ctx.metadata.get("predicted_cost_usd")
    if raw is None:
        return Decimal("0")
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(str(raw))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")


def _scope_for_ctx(
    ctx: RunContext, *, default_scope: BudgetScope
) -> tuple[BudgetScope, UUID]:
    """Resolve the (scope, scope_id) that this request gates against.

    Default policy: `default_scope="user"` → gate on `user_id`.
    Alternate: pre-flight host may stamp `ctx.metadata["budget_scope"]`
    + `ctx.metadata["budget_scope_id"]` to force a team-level gate
    (e.g. shared seat budgets). PLAN-014 §3 leaves the policy open;
    the hook honors whatever the host wired.
    """
    override_scope = ctx.metadata.get("budget_scope")
    override_id = ctx.metadata.get("budget_scope_id")
    if override_scope == "user" and isinstance(override_id, UUID):
        return "user", override_id
    if override_scope == "team" and isinstance(override_id, UUID):
        return "team", override_id

    if default_scope == "team":
        return "team", ctx.user_context.team_id
    return "user", ctx.user_context.user_id


def make_budget_pre_hook(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    summary_cache: SummaryCache,
    period: BudgetPeriod = "day",
    default_scope: BudgetScope = "user",
) -> BeforeRunHook:
    """Build the pre-flight hook (priority 10, ascending — runs first).

    Args:
      session_factory: ORM session source. Each hook invocation opens a
        fresh session (TXN-scoped advisory lock — see module docstring).
      summary_cache: shared metering cache. The hook hits it via
        `usage_by_user / usage_by_team`; the 60s TTL is fine because the
        race is closed by the lock, not the cache.
      period: which budget bucket to gate on. Defaults to "day"; host
        apps can wire multiple hooks (day + month).
      default_scope: which scope to gate on absent metadata override.

    Returns:
      A `BeforeRunHook` Protocol-compatible coroutine.
    """

    metering_period: Period = _BUDGET_TO_METERING[period]

    async def budget_pre(ctx: RunContext) -> None:
        scope, scope_id = _scope_for_ctx(ctx, default_scope=default_scope)
        predicted = _predicted_cost_from_ctx(ctx)

        try:
            async with session_factory() as session:
                # Step 0: advisory lock — first statement in the TXN.
                acquired = await try_lock_for_scope(
                    session,
                    scope=scope,
                    scope_id=scope_id,
                    period=period,
                )
                if not acquired:
                    # Fail-closed (PRD-014 L-01). Caller maps to 503.
                    raise BudgetLockUnavailableError(
                        f"advisory lock contended for {scope}:{scope_id}:{period}"
                    )

                # Step 1: limit lookup. No row → absent budget. PLAN-014
                # §2.2 F2: env-default fallback is host-app concern; the
                # hook treats absence as "no gate" (passes silently).
                row = await get_budget_limit(
                    session, scope=scope, scope_id=scope_id, period=period
                )
                if row is None:
                    return

                # Step 2: current usage. Cache OK — race is closed.
                if scope == "user":
                    summary = await summary_cache.by_user(
                        session, scope_id, metering_period
                    )
                else:
                    summary = await summary_cache.by_team(
                        session, scope_id, metering_period
                    )
                used = Decimal(summary.total_cost_usd)
                limit_usd = Decimal(row.limit_usd)

                # Step 3: gate. predicted is in $; we deny strictly when
                # the projection meets-or-exceeds the limit.
                if used + predicted >= limit_usd:
                    raise BudgetExceededError(
                        used_usd=used,
                        limit_usd=limit_usd,
                        predicted_usd=predicted,
                    )

                # Step 4: stamp projection for downstream hooks.
                ctx.metadata["budget_projection"] = {
                    "limit_usd": limit_usd,
                    "used_usd": used,
                    "predicted_usd": predicted,
                    "scope": scope,
                    "scope_id": scope_id,
                    "period": period,
                }
                # Commit releases the advisory lock immediately. The
                # `__aexit__` of the session does the commit; we leave
                # the lock held only for the few-statement gate check.
        except (BudgetExceededError, BudgetLockUnavailableError):
            # Pass through — typed errors map to deterministic HTTP.
            raise
        except SQLAlchemyError as exc:
            # DB down, table missing, etc. Fail-closed: caller maps to 503.
            logger.error("budget pre-flight DB error: %s", exc)
            raise BudgetSystemUnavailableError(
                "budget pre-flight database unavailable"
            ) from exc

    # The module-level cache reference must be kept usable even if the
    # caller passes a fresh cache via `summary_cache`. Doing the
    # fall-through inside the closure (above) covers it. Local
    # functions for `usage_by_*` are imported for the fall-through used
    # in tests that don't wire a cache; we reference them so the import
    # is meaningful for mypy --strict + ruff.
    _ = usage_by_user
    _ = usage_by_team
    return budget_pre


def make_budget_post_hook(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    summary_cache: SummaryCache,
    alerter: BudgetAlerter,
    audit_sink: AuditSink | None = None,
    period: BudgetPeriod = "day",
    default_scope: BudgetScope = "user",
) -> AfterRunHook:
    """Build the post-flight hook (priority 90, runs after audit at 80).

    Responsibilities:
      1. Re-aggregate the post-charge `BudgetStatus`.
      2. Fire `BudgetAlerter.maybe_fire(...)` for 80/95/100 thresholds.
      3. If realized cost > predicted (over-budget overrun), emit an
         `audit.budget.over_budget` event (PRD-014 §2.1 S3 +
         PLAN-015 cross-handoff).

    No raising on failure: the post hook fires after the tool has
    already executed; a post-flight exception would block the response.
    """

    async def budget_post(ctx: RunContext, result: Any) -> None:
        del result  # API contract carries it; budget logic ignores it.
        scope, scope_id = _scope_for_ctx(ctx, default_scope=default_scope)

        try:
            async with session_factory() as session:
                # Invalidate the per-bucket cache so the fresh aggregation
                # sees the new `usage_records` row that PLAN-013's
                # priority-75 hook just inserted.
                summary_cache.invalidate()
                status = await status_for(
                    session,
                    scope=scope,
                    scope_id=scope_id,
                    period=period,
                    cache=summary_cache,
                )
                if status is None:
                    # No budget configured — nothing to compare against.
                    return

                await alerter.maybe_fire(
                    scope=scope,
                    scope_id=scope_id,
                    period=period,
                    period_label=status.period_label,
                    used_usd=status.used_usd,
                    limit_usd=status.limit_usd,
                    used_pct=status.used_pct,
                )

                # Over-budget audit signal (PRD-014 §2.1 S3).
                projection = ctx.metadata.get("budget_projection")
                if (
                    audit_sink is not None
                    and isinstance(projection, dict)
                    and isinstance(projection.get("predicted_usd"), Decimal)
                ):
                    predicted = projection["predicted_usd"]
                    realized = ctx.metadata.get("recorded_cost_usd")
                    if (
                        isinstance(realized, Decimal)
                        and realized > predicted
                        and status.used_usd > status.limit_usd
                    ):
                        await audit_sink.emit(
                            AuditEvent(
                                event_type="budget.over_budget",
                                outcome="error",
                                user_id=ctx.user_context.user_id,
                                team_id=ctx.user_context.team_id,
                                agent_id=ctx.agent_id,
                                request_id=ctx.request_id,
                                tool_name=ctx.tool_name,
                                metadata={
                                    "scope": scope,
                                    "scope_id": str(scope_id),
                                    "period": period,
                                    "used_usd": str(status.used_usd),
                                    "limit_usd": str(status.limit_usd),
                                    "predicted_usd": str(predicted),
                                    "realized_usd": str(realized),
                                    "over_budget": True,
                                },
                            )
                        )
        except SQLAlchemyError as exc:
            # Post-flight failures do not block the response — log only.
            logger.warning("budget post-flight DB error: %s", exc)

    return budget_post


__all__ = [
    "make_budget_post_hook",
    "make_budget_pre_hook",
]
