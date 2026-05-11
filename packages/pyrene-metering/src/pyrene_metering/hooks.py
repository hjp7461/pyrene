"""`after_run` hook factory for cost metering.

PLAN-013 Day 1. Registers at `priority=PRIORITY_AUDIT - 5 = 75` so the
cost row is in place before the audit emit at 80 (PLAN-015) reads
metadata (an audit event may want to reference the recorded
`cost_usd` later — pre-positioning makes that read deterministic).

### Token extraction (ADR-002 D4)

The hook accepts an optional `usage_extractor` so test harnesses and
non-Pydantic-AI callers can plug in. The default extractor reads from
`ctx.metadata["usage"]` (a `RunUsage`-compatible mapping) — the
Gateway's pydantic-ai integration is expected to deposit it there after
`agent.run()` resolves. If absent, falls back to zero tokens (the hook
records a zero-cost row rather than failing — F-02 in PRD-013, dual
source: Logfire span carries the same numbers).

### Idempotency

The DB has a `UNIQUE(request_id, attempt_idx)` constraint. The hook
catches `IntegrityError` and demotes to a warning: the duplicate
indicates either (a) a concurrent retry race (winner already wrote;
loser observes the winner's row, no action) or (b) a PLAN-003 re-emission
bug (logged for triage). Either way the audit emit at priority 80 is
not blocked.

### attempt_idx source

PLAN-003's retry wrapper stamps `ctx.metadata["attempt_idx"]` (int) before
calling the gateway each retry. Default is 0 when the key is absent
(non-retried path). The hook does not introspect the retry wrapper
itself — that coupling lives in the test fixture.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyrene_gateway import AfterRunHook
from pyrene_gateway.context import RunContext
from pyrene_metering.pricing import PricingTable
from pyrene_metering.repository import insert_usage_record

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenUsage:
    """Minimal usage shape — matches Pydantic AI `RunUsage` fields.

    A separate dataclass (rather than re-using `RunUsage` directly)
    keeps the hook decoupled from pydantic-ai's internal layout — if
    `RunUsage` changes in 2.x the extractor adapter changes in one
    place.
    """

    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


# Pluggable extractor signature. Returns None to skip metering for this
# run (e.g. tool-only call that didn't invoke a model).
UsageExtractor = Callable[[RunContext, Any], TokenUsage | None]


def default_usage_extractor(ctx: RunContext, result: Any) -> TokenUsage | None:
    """Default extractor: pulls `ctx.metadata["usage"]` if present.

    Accepts either a dict-like mapping with the canonical keys or an
    object exposing the same attribute names (duck typing — works for
    both `RunUsage` instances and test fakes).
    """
    raw = ctx.metadata.get("usage")
    if raw is None:
        return None

    model = ctx.metadata.get("model")
    if not isinstance(model, str) or not model:
        return None

    def _get(key: str) -> int:
        val = raw.get(key, 0) if isinstance(raw, dict) else getattr(raw, key, 0)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    return TokenUsage(
        model=model,
        input_tokens=_get("input_tokens"),
        output_tokens=_get("output_tokens"),
        cache_read_tokens=_get("cache_read_tokens"),
        cache_write_tokens=_get("cache_write_tokens"),
    )


def _attempt_idx(ctx: RunContext) -> int:
    """Read `attempt_idx` from `ctx.metadata`; default 0."""
    raw = ctx.metadata.get("attempt_idx", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def make_cost_hook(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    pricing: PricingTable,
    usage_extractor: UsageExtractor = default_usage_extractor,
) -> AfterRunHook:
    """Build an `after_run` hook closed over a session factory + pricing.

    Registered as:

        gateway.after_run(make_cost_hook(...), priority=PRIORITY_AUDIT - 5)

    The factory takes an `async_sessionmaker` (not a bare `AsyncSession`)
    so each invocation gets its own session — the hook is invoked many
    times across the gateway's lifetime, and reusing a session would
    leak transactions / risk cross-request state.
    """

    async def cost_hook(ctx: RunContext, result: Any) -> None:
        usage = usage_extractor(ctx, result)
        if usage is None:
            # Nothing to record — non-model tool, missing metadata, etc.
            # Not an error; audit hook at priority 80 still runs.
            return

        cost = pricing.compute_cost(
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

        attempt_idx = _attempt_idx(ctx)
        async with session_factory() as session:
            try:
                await insert_usage_record(
                    session,
                    request_id=ctx.request_id,
                    attempt_idx=attempt_idx,
                    user_id=ctx.user_context.user_id,
                    team_id=ctx.user_context.team_id,
                    agent_id=ctx.agent_id,
                    model=usage.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cost_usd=cost,
                )
                await session.commit()
            except IntegrityError:
                # Race-induced duplicate (UNIQUE(request_id, attempt_idx)).
                # The winner already wrote; loser drops out silently
                # with a warning. F-02 dual source: Logfire span still
                # has the same numbers, so observability is intact.
                await session.rollback()
                logger.warning(
                    "metering: duplicate usage row for "
                    "(request_id=%s, attempt_idx=%d) — race winner already wrote",
                    ctx.request_id,
                    attempt_idx,
                )
                return

        # Echo the recorded cost into metadata so PRIORITY_AUDIT (80)
        # can read it without a second DB roundtrip if it wants to.
        ctx.metadata["recorded_cost_usd"] = cost

    return cost_hook


# Type-only re-export for callers that want to type their hook variable
# explicitly. `make_cost_hook` returns an `AfterRunHook`-shaped Callable.
CostHook = Callable[[RunContext, Any], Awaitable[None]]


__all__ = [
    "CostHook",
    "TokenUsage",
    "UsageExtractor",
    "default_usage_extractor",
    "make_cost_hook",
]
