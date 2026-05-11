"""Integration: `after_run` hook writes one row per agent invocation.

Drives the Gateway with a stub agent so the hook chain executes
end-to-end. Verifies:
  - One row inserted with correct user/team/agent/model.
  - `cost_usd` equals `pricing.compute_cost(...)` exactly.
  - The hook tolerates duplicate INSERT (race) without re-raising.
  - The cost hook runs BEFORE the audit hook (priority 75 < 80).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
)

from pyrene_core import UserContext
from pyrene_gateway import PRIORITY_AUDIT, Gateway
from pyrene_gateway.context import RunContext
from pyrene_metering import (
    PricingTable,
    TokenUsage,
    UsageRecord,
    default_pricing_path,
    make_cost_hook,
)

pytestmark = pytest.mark.integration


async def _seed(engine: AsyncEngine) -> tuple[UUID, UUID]:
    from sqlalchemy import text

    user_id = uuid4()
    team_id = uuid4()
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active) "
                "VALUES (:id, :email, :pw, TRUE)"
            ),
            {"id": user_id, "email": f"hook-{user_id}@x.test", "pw": "x"},
        )
        await s.execute(
            text("INSERT INTO teams (id, name) VALUES (:id, :name)"),
            {"id": team_id, "name": f"hook-{team_id}"},
        )
        await s.commit()
    return user_id, team_id


async def _cleanup(engine: AsyncEngine, user_id: UUID, team_id: UUID) -> None:
    from sqlalchemy import text

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(
            text("DELETE FROM usage_records WHERE user_id = :id"),
            {"id": user_id},
        )
        await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await s.execute(text("DELETE FROM teams WHERE id = :id"), {"id": team_id})
        await s.commit()


async def test_hook_inserts_one_row_after_agent_run(engine: AsyncEngine) -> None:
    """End-to-end: Gateway.run → after_run hook → usage_records row exists."""
    user_id, team_id = await _seed(engine)
    pricing = PricingTable(default_pricing_path())
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    # Pluggable extractor: deposit fixed usage so the test is deterministic.
    fixed_usage = TokenUsage(
        model="anthropic:claude-sonnet-4-6",
        input_tokens=10_000,
        output_tokens=20_000,
        cache_read_tokens=500,
        cache_write_tokens=0,
    )

    def extractor(ctx: RunContext, result: Any) -> TokenUsage:
        return fixed_usage

    hook = make_cost_hook(
        session_factory=factory, pricing=pricing, usage_extractor=extractor
    )
    gateway = Gateway()
    gateway.after_run(hook, priority=PRIORITY_AUDIT - 5)

    # Stub agent via pydantic-ai TestModel — no live network.
    from pydantic_ai import Agent

    agent: Agent[None, str] = Agent(
        TestModel(custom_output_text="ok"), deps_type=type(None), output_type=str
    )

    user_ctx = UserContext(user_id=user_id, team_id=team_id, roles=("analyst",))
    agent_id = uuid4()
    try:
        result = await gateway.run(
            agent,
            deps=None,
            user_context=user_ctx,
            question="ping",
            agent_id=agent_id,
        )
        assert result == "ok"

        async with factory() as s:
            rows = await s.execute(
                select(UsageRecord).where(UsageRecord.user_id == user_id)
            )
            records = rows.scalars().all()
        assert len(records) == 1
        row = records[0]
        assert row.team_id == team_id
        assert row.agent_id == agent_id
        assert row.model == "anthropic:claude-sonnet-4-6"
        assert row.input_tokens == 10_000
        assert row.output_tokens == 20_000
        assert row.cache_read_tokens == 500
        assert row.attempt_idx == 0

        expected_cost = pricing.compute_cost(
            model="anthropic:claude-sonnet-4-6",
            input_tokens=10_000,
            output_tokens=20_000,
            cache_read_tokens=500,
            cache_write_tokens=0,
        )
        assert row.cost_usd == expected_cost
        # And the metadata echo path stamped recorded_cost_usd.
    finally:
        await _cleanup(engine, user_id, team_id)


async def test_hook_swallows_race_duplicate(engine: AsyncEngine) -> None:
    """Second invocation with same (request_id, attempt_idx) → warning, no raise."""
    user_id, team_id = await _seed(engine)
    pricing = PricingTable(default_pricing_path())
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    fixed_usage = TokenUsage(
        model="anthropic:claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=200,
    )

    def extractor(ctx: RunContext, result: Any) -> TokenUsage:
        return fixed_usage

    hook = make_cost_hook(
        session_factory=factory, pricing=pricing, usage_extractor=extractor
    )

    pinned_request_id = uuid4()
    user_ctx = UserContext(user_id=user_id, team_id=team_id, roles=("analyst",))

    ctx = RunContext(
        user_context=user_ctx,
        request_id=pinned_request_id,
        agent_id=uuid4(),
        metadata={},
    )
    # First call → row inserted.
    try:
        await hook(ctx, result="ok")
        # Second call with same request_id + attempt_idx → IntegrityError
        # is swallowed inside the hook (logged as a warning).
        await hook(ctx, result="ok")

        async with factory() as s:
            rows = await s.execute(
                select(UsageRecord).where(UsageRecord.request_id == pinned_request_id)
            )
            records = rows.scalars().all()
        assert len(records) == 1
    finally:
        await _cleanup(engine, user_id, team_id)


async def test_hook_priority_runs_before_audit(engine: AsyncEngine) -> None:
    """In a 2-hook gateway, cost (75) executes before audit (80)."""
    user_id, team_id = await _seed(engine)
    pricing = PricingTable(default_pricing_path())
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    order: list[str] = []

    fixed_usage = TokenUsage(
        model="anthropic:claude-sonnet-4-6",
        input_tokens=1,
        output_tokens=1,
    )

    def extractor(ctx: RunContext, result: Any) -> TokenUsage:
        order.append("cost")
        return fixed_usage

    async def audit_hook(ctx: RunContext, result: Any) -> None:
        order.append("audit")
        # The cost hook stamps `recorded_cost_usd` — audit observes it.
        assert "recorded_cost_usd" in ctx.metadata
        assert isinstance(ctx.metadata["recorded_cost_usd"], Decimal)

    cost_hook = make_cost_hook(
        session_factory=factory, pricing=pricing, usage_extractor=extractor
    )

    gateway = Gateway()
    # Register in REVERSE priority order to prove sort dominates.
    gateway.after_run(audit_hook, priority=PRIORITY_AUDIT)
    gateway.after_run(cost_hook, priority=PRIORITY_AUDIT - 5)

    from pydantic_ai import Agent

    agent: Agent[None, str] = Agent(
        TestModel(custom_output_text="ok"), deps_type=type(None), output_type=str
    )
    user_ctx = UserContext(user_id=user_id, team_id=team_id, roles=("analyst",))

    try:
        await gateway.run(
            agent,
            deps=None,
            user_context=user_ctx,
            question="ping",
        )
        assert order == ["cost", "audit"]
    finally:
        await _cleanup(engine, user_id, team_id)
