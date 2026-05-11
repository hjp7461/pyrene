"""HookRegistry: priority sort + tie-break + Protocol satisfaction.

PLAN-009 Day 3 completion criteria:
  - hooks execute in ascending priority order regardless of registration order;
  - same-priority hooks execute in insertion order (stable sort tie-break);
  - `_StubAuditSink` wrapped as an after-hook is callable and runtime-checkable
    as `AfterRunHook` (Protocol satisfaction).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from pyrene_core import AuditEvent, UserContext, _StubAuditSink
from pyrene_gateway import (
    PRIORITY_AUDIT,
    PRIORITY_BUDGET_POST,
    PRIORITY_BUDGET_PRE,
    PRIORITY_DATA_RBAC,
    PRIORITY_TOOL_RBAC,
    AfterRunHook,
    BeforeRunHook,
    HookRegistry,
    RunContext,
)


def _make_ctx() -> RunContext:
    return RunContext(
        user_context=UserContext(
            user_id=uuid4(),
            team_id=uuid4(),
            roles=("analyst",),
        ),
        request_id=uuid4(),
    )


async def test_before_hooks_run_in_priority_order() -> None:
    """Hooks registered with different priorities execute ascending."""
    log: list[int] = []
    registry = HookRegistry()

    async def h30(ctx: RunContext) -> None:
        log.append(30)

    async def h10(ctx: RunContext) -> None:
        log.append(10)

    async def h20(ctx: RunContext) -> None:
        log.append(20)

    # Register in mixed order.
    registry.register_before(h30, priority=30)
    registry.register_before(h10, priority=10)
    registry.register_before(h20, priority=20)

    ctx = _make_ctx()
    for hook in registry.before_hooks():
        await hook(ctx)

    assert log == [10, 20, 30]


async def test_same_priority_runs_in_insertion_order() -> None:
    """Tie-break: same priority, insertion order preserved (stable sort)."""
    log: list[str] = []
    registry = HookRegistry()

    async def h_a(ctx: RunContext) -> None:
        log.append("a")

    async def h_b(ctx: RunContext) -> None:
        log.append("b")

    async def h_c(ctx: RunContext) -> None:
        log.append("c")

    registry.register_before(h_a, priority=20)
    registry.register_before(h_b, priority=20)
    registry.register_before(h_c, priority=20)

    ctx = _make_ctx()
    for hook in registry.before_hooks():
        await hook(ctx)

    assert log == ["a", "b", "c"]


async def test_mixed_priority_and_tie_break() -> None:
    """Combined: different priorities + same-priority preserves insertion order."""
    log: list[str] = []
    registry = HookRegistry()

    async def h_x(ctx: RunContext) -> None:
        log.append("x@30")

    async def h_y(ctx: RunContext) -> None:
        log.append("y@10a")

    async def h_z(ctx: RunContext) -> None:
        log.append("z@10b")

    registry.register_before(h_x, priority=30)
    registry.register_before(h_y, priority=10)
    registry.register_before(h_z, priority=10)

    ctx = _make_ctx()
    for hook in registry.before_hooks():
        await hook(ctx)

    # y registered before z at priority 10; both run before x.
    assert log == ["y@10a", "z@10b", "x@30"]


async def test_after_hooks_run_ascending_too() -> None:
    """`after_run` is ALSO ascending — PRD-009 §C-2 (no reverse on after)."""
    log: list[int] = []
    registry = HookRegistry()

    async def a90(ctx: RunContext, result: Any) -> None:
        log.append(90)

    async def a80(ctx: RunContext, result: Any) -> None:
        log.append(80)

    registry.register_after(a90, priority=90)
    registry.register_after(a80, priority=80)

    ctx = _make_ctx()
    for hook in registry.after_hooks():
        await hook(ctx, None)

    assert log == [80, 90]


async def test_hook_propagates_exception() -> None:
    """Fail-closed: hook raise propagates, no silent swallow."""
    registry = HookRegistry()

    async def fail_hook(ctx: RunContext) -> None:
        raise RuntimeError("vetoed")

    registry.register_before(fail_hook, priority=20)

    ctx = _make_ctx()
    with pytest.raises(RuntimeError, match="vetoed"):
        for hook in registry.before_hooks():
            await hook(ctx)


def test_protocol_runtime_check_before() -> None:
    """BeforeRunHook is runtime_checkable — async functions satisfy it."""

    async def h(ctx: RunContext) -> None:
        return None

    assert isinstance(h, BeforeRunHook)


def test_protocol_runtime_check_after() -> None:
    async def h(ctx: RunContext, result: Any) -> None:
        return None

    assert isinstance(h, AfterRunHook)


async def test_canonical_five_stage_chain() -> None:
    """The 5 PRIORITY_* constants compose the canonical before+after order.

    Stage B §C-2: budget-pre(10) → tool-rbac(20) → data-rbac(30) → tool →
    audit(80) → budget-post(90). This test asserts the chain is observable
    end-to-end with each priority constant exercised once.
    """
    log: list[int] = []
    registry = HookRegistry()

    async def budget_pre(ctx: RunContext) -> None:
        log.append(PRIORITY_BUDGET_PRE)

    async def tool_rbac(ctx: RunContext) -> None:
        log.append(PRIORITY_TOOL_RBAC)

    async def data_rbac(ctx: RunContext) -> None:
        log.append(PRIORITY_DATA_RBAC)

    async def audit_emit(ctx: RunContext, result: Any) -> None:
        log.append(PRIORITY_AUDIT)

    async def budget_post(ctx: RunContext, result: Any) -> None:
        log.append(PRIORITY_BUDGET_POST)

    # Intentionally register out-of-order to prove priority sorts independently.
    registry.register_before(data_rbac, priority=PRIORITY_DATA_RBAC)
    registry.register_before(budget_pre, priority=PRIORITY_BUDGET_PRE)
    registry.register_before(tool_rbac, priority=PRIORITY_TOOL_RBAC)
    registry.register_after(budget_post, priority=PRIORITY_BUDGET_POST)
    registry.register_after(audit_emit, priority=PRIORITY_AUDIT)

    ctx = _make_ctx()
    for before in registry.before_hooks():
        await before(ctx)
    # tool exec is the implicit gap between before and after — represent as
    # a sentinel.
    log.append(50)
    for after in registry.after_hooks():
        await after(ctx, None)

    assert log == [
        PRIORITY_BUDGET_PRE,
        PRIORITY_TOOL_RBAC,
        PRIORITY_DATA_RBAC,
        50,
        PRIORITY_AUDIT,
        PRIORITY_BUDGET_POST,
    ]


async def test_stub_audit_sink_as_hook() -> None:
    """`_StubAuditSink.emit` wrapped as an `AfterRunHook` is callable."""
    sink = _StubAuditSink()

    async def audit_hook(ctx: RunContext, result: Any) -> None:
        await sink.emit(
            AuditEvent(
                event_type="tool_call",
                outcome="allowed",
                user_id=ctx.user_context.user_id,
                team_id=ctx.user_context.team_id,
                request_id=ctx.request_id,
            )
        )

    assert isinstance(audit_hook, AfterRunHook)

    registry = HookRegistry()
    registry.register_after(audit_hook, priority=PRIORITY_AUDIT)

    ctx = _make_ctx()
    for hook in registry.after_hooks():
        await hook(ctx, None)

    assert sink.emit_count == 1
