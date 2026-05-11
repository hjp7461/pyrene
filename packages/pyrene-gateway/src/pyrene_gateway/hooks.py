"""Hook system for the MCP gateway.

PLAN-009 Day 3. Defines the canonical 5-stage chain shared by
PLAN-010 (tool RBAC), PLAN-011 (data RBAC), PLAN-013/014 (cost/budget),
PLAN-015 (audit):

```
                                                       (tool execution
PRIORITY_BUDGET_PRE = 10                                 between before
PRIORITY_TOOL_RBAC  = 20                                 and after)
PRIORITY_DATA_RBAC  = 30
   <tool runs>
PRIORITY_AUDIT       = 80
PRIORITY_BUDGET_POST = 90
```

### Why two Protocols (before vs after)

`AfterRunHook` receives the agent result. `BeforeRunHook` only sees the
context (no result yet). A single Protocol with `result: AgentResult |
None` would force every before-hook to guard on `None`. Two Protocols
keeps the signatures tight and surfaces type errors at registration.

### Priority ordering

Both `before_run` and `after_run` execute **ascending** priority — no
reverse. This is a deliberate Stage B §C-2 decision: audit must observe
the same logical event whether it ran before or after the tool, and
forcing reverse on after_run would make budget-post (90) run before
audit (80), which is the opposite of what we want.

Insertion order breaks ties. Python's `list.sort` is stable, so two
hooks registered with `priority=20` execute in the order they were
registered.

### Fail-closed

If any hook `await hook(ctx, ...)` raises, the gateway propagates the
exception out of `Gateway.run(...)`. Tool RBAC and budget gates rely on
this — silently swallowing means the tool runs without authorization.
PLAN-015 audit hook implementations MUST log + swallow at the sink layer
(the gateway records `outcome="error"` only when the tool itself raises,
not when a hook intentionally vetoes).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pyrene_gateway.context import RunContext


@runtime_checkable
class BeforeRunHook(Protocol):
    """Hook called before tool execution.

    Receives the `RunContext` only. Raise from this hook to veto the
    run; the gateway re-raises to the caller (fail-closed).
    """

    async def __call__(self, ctx: RunContext) -> None: ...


@runtime_checkable
class AfterRunHook(Protocol):
    """Hook called after tool execution.

    Receives `(ctx, result)`. `result` is the agent's structured output
    (Pydantic model). After-hooks SHOULD NOT mutate `result`; the
    gateway re-raises any exception they raise.
    """

    async def __call__(self, ctx: RunContext, result: Any) -> None: ...


# Internal record. Public API only exposes the registration methods on
# `Gateway` — callers never touch HookEntry directly.
@dataclass(frozen=True)
class _HookEntry[F]:
    """One registered hook + its priority + insertion order.

    `seq` is the registry's insertion counter so stable sort by
    `(priority, seq)` gives deterministic ordering even when the same
    priority is registered multiple times.
    """

    priority: int
    seq: int
    hook: F


# Type aliases to keep the registry signatures readable.
_BeforeFn = Callable[[RunContext], Awaitable[None]]
_AfterFn = Callable[[RunContext, Any], Awaitable[None]]


@dataclass
class HookRegistry:
    """Ordered store of before/after hooks.

    Public methods (`register_before`, `register_after`, `before_run`,
    `after_run`) are exposed via the `Gateway` facade so plan authors
    do not need to know the internal storage details.

    Storage uses `list[_HookEntry[...]]` rather than `sortedcontainers`
    because (a) Python list.sort is stable, (b) the typical registry
    holds 5-10 hooks total, (c) registration is a startup-time event,
    not a hot path — re-sorting on each register is fine.
    """

    _before: list[_HookEntry[_BeforeFn]] = field(default_factory=list)
    _after: list[_HookEntry[_AfterFn]] = field(default_factory=list)
    _counter: int = 0

    def _next_seq(self) -> int:
        self._counter += 1
        return self._counter

    def register_before(self, hook: BeforeRunHook, *, priority: int) -> None:
        """Add a before-run hook.

        `priority`: lower runs first. `Gateway.before_run(...)` is the
        public-facing alias.
        """
        # Pydantic-AI / mypy-strict friendly: cast the Protocol-typed
        # hook into the underlying Callable signature. Structural
        # typing guarantees the cast is safe.
        entry: _HookEntry[_BeforeFn] = _HookEntry(
            priority=priority, seq=self._next_seq(), hook=hook
        )
        self._before.append(entry)
        # Stable sort — equal-priority entries keep registration order.
        self._before.sort(key=lambda e: (e.priority, e.seq))

    def register_after(self, hook: AfterRunHook, *, priority: int) -> None:
        """Add an after-run hook (same priority semantics as `register_before`)."""
        entry: _HookEntry[_AfterFn] = _HookEntry(
            priority=priority, seq=self._next_seq(), hook=hook
        )
        self._after.append(entry)
        self._after.sort(key=lambda e: (e.priority, e.seq))

    def before_hooks(self) -> tuple[BeforeRunHook, ...]:
        """Return current before-run chain in execution order (ascending).

        The stored `Callable` and the `BeforeRunHook` Protocol are
        structurally compatible — Protocol with `__call__` matches a
        bare async function. We cast at the boundary so mypy --strict
        narrows the return type for callers.
        """
        from typing import cast

        return tuple(cast(BeforeRunHook, e.hook) for e in self._before)

    def after_hooks(self) -> tuple[AfterRunHook, ...]:
        """Return current after-run chain in execution order (ascending)."""
        from typing import cast

        return tuple(cast(AfterRunHook, e.hook) for e in self._after)


__all__ = [
    "AfterRunHook",
    "BeforeRunHook",
    "HookRegistry",
]
