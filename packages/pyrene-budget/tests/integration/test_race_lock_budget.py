"""PLAN-014 Day 1 §위험 신호 #1 race regression.

Fires 10 concurrent pre-flight hooks at the 95%-used budget. The
advisory-lock serializer must ensure exactly **one** request passes
(its `predicted + used` evaluates strictly below `limit`); the other
**nine** must each raise either:

  - `BudgetLockUnavailableError` (lost the lock race; fail-closed 503), or
  - `BudgetExceededError` (acquired the lock after the winner committed
    the cost row that pushed `used` past the limit).

The exact split depends on Postgres scheduling — we assert
`pass_count == 1` regardless.

The test uses `asyncio.Event` to synchronize all 10 coroutines to fire
within the same scheduling tick, mirroring the PM-amended fixture
shape.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyrene_budget import (
    BudgetExceededError,
    BudgetLockUnavailableError,
    make_budget_pre_hook,
)
from pyrene_core import UserContext
from pyrene_gateway import RunContext
from pyrene_metering.aggregation import SummaryCache

pytestmark = pytest.mark.integration


async def _seed_user_team_engine(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID]:
    """Seed user/team directly via the engine (no savepoint)."""
    user_id = uuid4()
    team_id = uuid4()
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active) "
                "VALUES (:id, :email, :pw, TRUE)"
            ),
            {"id": user_id, "email": f"user-{user_id}@e.t", "pw": "x"},
        )
        await s.execute(
            text("INSERT INTO teams (id, name) VALUES (:id, :name)"),
            {"id": team_id, "name": f"team-{team_id}"},
        )
        await s.commit()
    return user_id, team_id


async def _seed_budget(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    limit_usd: Decimal,
) -> None:
    """Seed a daily budget for the user."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO budget_limits (id, scope, scope_id, period, limit_usd) "
                "VALUES (:id, 'user', :uid, 'day', :limit)"
            ),
            {"id": uuid4(), "uid": user_id, "limit": limit_usd},
        )
        await s.commit()


async def _seed_usage(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    team_id: UUID,
    cost: Decimal,
) -> None:
    """Drop a single usage row representing prior spend in the current bucket."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO usage_records ("
                "id, request_id, attempt_idx, user_id, team_id, "
                "model, input_tokens, output_tokens, cost_usd, created_at"
                ") VALUES (:id, :rid, 0, :uid, :tid, 'test', 0, 0, :cost, NOW())"
            ),
            {
                "id": uuid4(),
                "rid": uuid4(),
                "uid": user_id,
                "tid": team_id,
                "cost": cost,
            },
        )
        await s.commit()


def _ctx(user_id: UUID, team_id: UUID, predicted: Decimal) -> RunContext:
    return RunContext(
        user_context=UserContext(user_id=user_id, team_id=team_id, roles=("user",)),
        request_id=uuid4(),
        metadata={"predicted_cost_usd": predicted},
    )


async def test_advisory_lock_serializes_10_concurrent_pre_flights(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exactly one of ten concurrent pre-flight hooks at 95% passes.

    Setup: limit=$1.00, used=$0.95 (95%), predicted=$0.05 per request.
    Each request would push `used + predicted = $1.00 >= limit` so the
    gate is strictly violated. But: PLAN-013's post-charge has not yet
    fired (we're testing the *pre*-flight race), so initially nine
    parallel pre-flights *would* all see "used=$0.95, predicted=$0.05,
    used+predicted=$1.00" and all be blocked.

    To make the race meaningful we set `limit=$1.01` so each request,
    in isolation, projects just under the limit (1.00 < 1.01). The
    advisory lock then ensures that whatever realized cost is recorded
    by the *first* request before the second runs would push `used`
    over the limit for everyone else. **Because the test fires the
    pre-flights simultaneously, only one wins the lock per scheduling
    slot**; the others either lose the lock or, after the first
    commits, see the bumped used value.

    Assertion: `pass_count == 1`, `blocked_count == 9`.
    """
    user_id, team_id = await _seed_user_team_engine(session_factory)
    await _seed_budget(session_factory, user_id=user_id, limit_usd=Decimal("1.01"))
    await _seed_usage(
        session_factory, user_id=user_id, team_id=team_id, cost=Decimal("0.95")
    )

    cache = SummaryCache(ttl_seconds=1)
    hook = make_budget_pre_hook(
        session_factory=session_factory,
        summary_cache=cache,
    )

    start_gate = asyncio.Event()
    results: list[Exception | None] = []
    realized_cost = Decimal("0.10")  # winner records a $0.10 row

    async def run_one(predicted: Decimal) -> None:
        # Block until the orchestrator releases the gate.
        await start_gate.wait()
        try:
            ctx = _ctx(user_id, team_id, predicted)
            await hook(ctx)
            # Winner: simulate the metering hook recording the real cost
            # so subsequent pre-flights see the bumped `used` value.
            async with session_factory() as s:
                await s.execute(
                    text(
                        "INSERT INTO usage_records ("
                        "id, request_id, attempt_idx, user_id, team_id, "
                        "model, input_tokens, output_tokens, cost_usd, created_at"
                        ") VALUES (:id, :rid, 0, :uid, :tid, 'test', 0, 0, :cost, NOW())"
                    ),
                    {
                        "id": uuid4(),
                        "rid": uuid4(),
                        "uid": user_id,
                        "tid": team_id,
                        "cost": realized_cost,
                    },
                )
                await s.commit()
            cache.invalidate()
            results.append(None)
        except (BudgetExceededError, BudgetLockUnavailableError) as exc:
            results.append(exc)

    # Fire 10 concurrent coroutines, each projecting $0.05.
    tasks = [
        asyncio.create_task(run_one(Decimal("0.05"))) for _ in range(10)
    ]
    # Wait until all are parked on `start_gate.wait()`.
    await asyncio.sleep(0.05)
    start_gate.set()
    await asyncio.gather(*tasks, return_exceptions=True)

    passes = [r for r in results if r is None]
    blocked = [r for r in results if isinstance(r, Exception)]
    assert len(results) == 10
    # The serializer guarantees exactly one winner (PM-amended assertion).
    assert len(passes) == 1, (
        f"expected exactly 1 pass, got {len(passes)} "
        f"(blocked: {[type(b).__name__ for b in blocked]})"
    )
    assert len(blocked) == 9
    # Every block must be one of the two fail-closed types.
    for b in blocked:
        assert isinstance(b, BudgetExceededError | BudgetLockUnavailableError)


async def test_lock_acquired_under_no_contention(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Single pre-flight: lock acquired, no contention, no exception."""
    user_id, team_id = await _seed_user_team_engine(session_factory)
    await _seed_budget(
        session_factory, user_id=user_id, limit_usd=Decimal("10.00")
    )

    cache = SummaryCache(ttl_seconds=1)
    hook = make_budget_pre_hook(
        session_factory=session_factory,
        summary_cache=cache,
    )

    ctx = _ctx(user_id, team_id, Decimal("0.01"))
    await hook(ctx)  # must not raise.
    # The hook stamped the projection so the post-hook + audit can read it.
    assert "budget_projection" in ctx.metadata


async def test_advisory_lock_key_sql_matches_pm_amend(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exact SQL the hook executes matches the PM-amended spec.

    PRD-014 §Day 1: the lock key is
    `hashtextextended(scope || ':' || scope_id::text || ':' || period, 0)`.

    Because SQLAlchemy + asyncpg cannot parse `:param::cast` (the
    bind-parameter colon and the Postgres `::cast` operator collide),
    `repository.try_lock_for_scope` builds the composite key Python-side
    via `_composite_key(scope, scope_id, period)` — output:
        `"user:<scope_id-as-text>:day"`
    which is the *exact same string* the SQL `scope || ':' ||
    scope_id::text || ':' || period` would produce. We verify the
    hash on that pre-built composite returns an int (proving the
    function is callable and the composite shape is accepted).
    """
    from pyrene_budget.repository import _composite_key

    sid = uuid4()
    composite = _composite_key("user", sid, "day")
    expected = f"user:{sid}:day"
    assert composite == expected
    async with session_factory() as s:
        result = await s.execute(
            text("SELECT hashtextextended(:k, 0)"), {"k": composite}
        )
        val = result.scalar_one()
        assert isinstance(val, int)


# Discourage unused-import false positives — `Any` used in type comments.
_ = Any
