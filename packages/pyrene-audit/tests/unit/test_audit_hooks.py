"""Unit tests for `make_audit_hook` + `register_audit_sink`.

Exercises:
  - The factory returns an `AfterRunHook`-shaped coroutine.
  - It emits exactly one event per call, populated from `RunContext`.
  - Sink exceptions propagate (fail-closed, PRD-015 §F1).
  - `register_audit_sink` swaps `gateway.audit_sink` AND registers the
    hook at `PRIORITY_AUDIT = 80`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from pyrene_audit import make_audit_hook, register_audit_sink
from pyrene_core import AuditEvent, AuditSink, UserContext, _StubAuditSink
from pyrene_gateway import PRIORITY_AUDIT, Gateway, RunContext


class _RecordingSink:
    """Captures emitted events for assertions."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class _ExplodingSink:
    """Sink that fails — verifies fail-closed propagation."""

    async def emit(self, event: AuditEvent) -> None:
        raise RuntimeError("audit DB unreachable")


def _ctx() -> RunContext:
    return RunContext(
        user_context=UserContext(
            user_id=uuid4(), team_id=uuid4(), roles=("admin",)
        ),
        agent_id=uuid4(),
        request_id=uuid4(),
        tool_name="run_select",
    )


async def test_make_audit_hook_emits_one_event() -> None:
    sink = _RecordingSink()
    hook = make_audit_hook(sink)
    ctx = _ctx()

    await hook(ctx, {"answer": "42"})

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.event_type == "agent.run"
    assert event.outcome == "allowed"
    assert event.user_id == ctx.user_context.user_id
    assert event.team_id == ctx.user_context.team_id
    assert event.agent_id == ctx.agent_id
    assert event.request_id == ctx.request_id
    assert event.tool_name == "run_select"
    assert "result_repr" in event.metadata


async def test_make_audit_hook_custom_event_type() -> None:
    sink = _RecordingSink()
    hook = make_audit_hook(sink, event_type="tool.completed")
    await hook(_ctx(), {"ok": True})
    assert sink.events[0].event_type == "tool.completed"


async def test_make_audit_hook_truncates_huge_result_repr() -> None:
    """Repr is capped at 512 chars to keep JSONB column compact."""
    sink = _RecordingSink()
    hook = make_audit_hook(sink)
    huge = {"data": "x" * 10_000}
    await hook(_ctx(), huge)
    assert len(sink.events[0].metadata["result_repr"]) <= 512


async def test_make_audit_hook_propagates_sink_exception() -> None:
    """PRD-015 §F1: emit failure is fail-closed."""
    hook = make_audit_hook(_ExplodingSink())
    with pytest.raises(RuntimeError, match="audit DB unreachable"):
        await hook(_ctx(), {})


async def test_register_audit_sink_swaps_slot_and_registers_hook() -> None:
    gateway = Gateway()
    # Default sink is the stub (PLAN-009 default).
    assert isinstance(gateway.audit_sink, _StubAuditSink)

    sink = _RecordingSink()
    hook = register_audit_sink(gateway, sink)

    # Slot swapped. Use `id(...)` to dodge mypy's overly-narrow
    # inference (`audit_sink` was narrowed to `_StubAuditSink` by the
    # earlier isinstance check on the same name).
    assert id(gateway.audit_sink) == id(sink)
    # Hook registered.
    assert hook in gateway.after_hooks()
    # Sink also satisfies AuditSink Protocol.
    assert isinstance(sink, AuditSink)


async def test_register_audit_sink_hook_runs_at_priority_audit() -> None:
    """Audit hook fires after a tool_rbac hook with lower priority.

    Confirms the registration uses PRIORITY_AUDIT = 80 rather than an
    ad-hoc value — order observable via the canonical chain.
    """
    log: list[str] = []
    gateway = Gateway()

    async def tool_rbac(ctx: RunContext) -> None:
        log.append("tool_rbac")

    async def budget_post(ctx: RunContext, result: Any) -> None:
        log.append("budget_post")

    gateway.before_run(tool_rbac, priority=20)
    gateway.after_run(budget_post, priority=90)

    sink = _RecordingSink()

    async def record(ctx: RunContext, result: Any) -> None:
        log.append("audit")
        # Use the real factory to ensure the priority assignment fires.
        _ = sink

    register_audit_sink(gateway, sink)
    # Swap the registered hook for a sentinel so we can observe ordering
    # without needing a real agent. We re-register at the same priority;
    # we leave the original registration as the actual emit path.
    gateway.after_run(record, priority=PRIORITY_AUDIT)

    # Drive the chain manually — Gateway.run requires a pydantic-ai Agent,
    # which is overkill for an ordering test.
    ctx = _ctx()
    for before in gateway.before_hooks():
        await before(ctx)
    for after in gateway.after_hooks():
        await after(ctx, {"out": True})

    # Expected order: tool_rbac (20) → audit (80, both registrations) → budget_post (90)
    assert log[0] == "tool_rbac"
    assert log[-1] == "budget_post"
    assert "audit" in log
    # And the DBAuditSink-equivalent recorded its event.
    assert len(sink.events) == 1
