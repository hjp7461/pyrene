"""Gateway.run() integration with a stubbed agent.

PLAN-009 Day 3. Exercises the full hook chain around a mocked
`pydantic_ai.Agent` so the integration with audit sink + priority
ordering is observed end-to-end without a model API call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from pyrene_core import AuditEvent, UserContext, _StubAuditSink
from pyrene_gateway import (
    PRIORITY_AUDIT,
    PRIORITY_BUDGET_POST,
    PRIORITY_BUDGET_PRE,
    PRIORITY_TOOL_RBAC,
    Gateway,
    RunContext,
)


@dataclass
class _FakeAgentResult:
    output: dict[str, Any]


class _FakeAgent:
    """Stub pydantic-ai Agent that returns a canned result."""

    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output
        self.last_question: str | None = None
        self.last_deps: object | None = None

    async def run(self, question: str, *, deps: object) -> _FakeAgentResult:
        self.last_question = question
        self.last_deps = deps
        return _FakeAgentResult(output=self._output)


def _user() -> UserContext:
    return UserContext(user_id=uuid4(), team_id=uuid4(), roles=("analyst",))


async def test_gateway_run_returns_agent_output() -> None:
    gateway = Gateway()
    agent = _FakeAgent({"answer": "42"})

    output = await gateway.run(
        agent,  # type: ignore[arg-type]
        deps=object(),
        user_context=_user(),
        question="what is meaning?",
    )

    assert output == {"answer": "42"}
    assert agent.last_question == "what is meaning?"


async def test_gateway_invokes_hooks_in_canonical_order() -> None:
    """Register all 5 stages and assert the gateway invokes them in order."""
    log: list[int] = []
    gateway = Gateway()

    async def budget_pre(ctx: RunContext) -> None:
        log.append(PRIORITY_BUDGET_PRE)

    async def tool_rbac(ctx: RunContext) -> None:
        log.append(PRIORITY_TOOL_RBAC)

    async def audit_emit(ctx: RunContext, result: Any) -> None:
        log.append(PRIORITY_AUDIT)

    async def budget_post(ctx: RunContext, result: Any) -> None:
        log.append(PRIORITY_BUDGET_POST)

    # Register in scrambled order to confirm priority dominates registration.
    gateway.after_run(budget_post, priority=PRIORITY_BUDGET_POST)
    gateway.before_run(tool_rbac, priority=PRIORITY_TOOL_RBAC)
    gateway.after_run(audit_emit, priority=PRIORITY_AUDIT)
    gateway.before_run(budget_pre, priority=PRIORITY_BUDGET_PRE)

    agent = _FakeAgent({"ok": True})
    await gateway.run(
        agent,  # type: ignore[arg-type]
        deps=object(),
        user_context=_user(),
        question="q",
    )

    assert log == [
        PRIORITY_BUDGET_PRE,
        PRIORITY_TOOL_RBAC,
        PRIORITY_AUDIT,
        PRIORITY_BUDGET_POST,
    ]


async def test_gateway_before_hook_veto_blocks_agent_call() -> None:
    """Fail-closed: a `before_run` raise prevents agent.run from running."""
    gateway = Gateway()
    agent = _FakeAgent({"unreachable": True})

    async def veto(ctx: RunContext) -> None:
        raise PermissionError("policy_denied")

    gateway.before_run(veto, priority=PRIORITY_TOOL_RBAC)

    with pytest.raises(PermissionError, match="policy_denied"):
        await gateway.run(
            agent,  # type: ignore[arg-type]
            deps=object(),
            user_context=_user(),
            question="q",
        )

    # Agent was never called.
    assert agent.last_question is None


async def test_gateway_audit_sink_swap_point() -> None:
    """Stub audit sink wired via constructor — PLAN-015 swap point.

    The gateway holds the sink; audit hooks close over `gateway.audit_sink`
    so PLAN-015 can replace the slot at app startup. Here we observe the
    chain by binding the stub at construction.
    """
    sink = _StubAuditSink()
    gateway = Gateway(audit_sink=sink)
    agent = _FakeAgent({"ok": True})

    async def audit_emit(ctx: RunContext, result: Any) -> None:
        await gateway.audit_sink.emit(
            AuditEvent(
                event_type="tool_call",
                outcome="allowed",
                user_id=ctx.user_context.user_id,
                team_id=ctx.user_context.team_id,
                request_id=ctx.request_id,
            )
        )

    gateway.after_run(audit_emit, priority=PRIORITY_AUDIT)

    await gateway.run(
        agent,  # type: ignore[arg-type]
        deps=object(),
        user_context=_user(),
        question="q",
    )

    assert sink.emit_count == 1


async def test_gateway_default_audit_sink_is_stub() -> None:
    """Default constructor uses `_StubAuditSink` (PLAN-009 default)."""
    gateway = Gateway()
    assert isinstance(gateway.audit_sink, _StubAuditSink)


async def test_gateway_inspection_apis() -> None:
    gateway = Gateway()

    async def b(ctx: RunContext) -> None:
        return None

    async def a(ctx: RunContext, result: Any) -> None:
        return None

    gateway.before_run(b, priority=10)
    gateway.after_run(a, priority=80)
    assert len(gateway.before_hooks()) == 1
    assert len(gateway.after_hooks()) == 1


async def test_gateway_request_id_per_run() -> None:
    """Each run gets a fresh UUID request_id unless caller pins one."""
    seen: list[Any] = []
    gateway = Gateway()

    async def capture(ctx: RunContext) -> None:
        seen.append(ctx.request_id)

    gateway.before_run(capture, priority=10)
    agent = _FakeAgent({})
    await gateway.run(
        agent,  # type: ignore[arg-type]
        deps=object(),
        user_context=_user(),
        question="q1",
    )
    await gateway.run(
        agent,  # type: ignore[arg-type]
        deps=object(),
        user_context=_user(),
        question="q2",
    )

    assert seen[0] != seen[1]


async def test_gateway_request_id_pinned_by_caller() -> None:
    """Caller-provided `request_id` is preserved (PLAN-015 cross-system join)."""
    seen: list[Any] = []
    gateway = Gateway()

    async def capture(ctx: RunContext) -> None:
        seen.append(ctx.request_id)

    gateway.before_run(capture, priority=10)
    agent = _FakeAgent({})
    pinned = uuid4()
    await gateway.run(
        agent,  # type: ignore[arg-type]
        deps=object(),
        user_context=_user(),
        question="q",
        request_id=pinned,
    )
    assert seen == [pinned]
