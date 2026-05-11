"""`Gateway` — orchestrates hook chain around agent / tool execution.

PLAN-009 Day 3. The gateway is the single entry point that wraps every
agent run with the canonical 5-stage chain (budget-pre → tool-RBAC →
data-RBAC → tool → audit → budget-post). PLAN-010/011/013/014/015
register their hooks against it at app startup; PLAN-009 itself ships
no policy hooks — just the registry and the executor.

### Lifecycle

```python
gateway = Gateway(audit_sink=_StubAuditSink())
gateway.before_run(my_budget_pre, priority=PRIORITY_BUDGET_PRE)
gateway.after_run(my_audit_emit, priority=PRIORITY_AUDIT)

result = await gateway.run(agent, deps=deps, question="...")
```

### `audit_sink` slot

The constructor accepts an `AuditSink` (default `_StubAuditSink()`).
PLAN-015 will pass a real `DBAuditSink`. The gateway itself does not
emit events — that's the audit hook's responsibility. The sink lives
on the gateway so audit hook closures can grab it via `gateway.audit_sink`
at registration time, keeping plan-009 unaware of plan-015 internals.

### Fail-closed

Any hook exception propagates out of `run(...)`. The caller (FastAPI
route in PLAN-009 Day 4) maps known categories to HTTP status codes.
The gateway does not retry — retry semantics belong to the SQL agent
wrapper (PLAN-003 `run_with_retry`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from pydantic_ai import Agent

from pyrene_core import AuditSink, UserContext, _StubAuditSink
from pyrene_gateway.context import RunContext
from pyrene_gateway.hooks import AfterRunHook, BeforeRunHook, HookRegistry


class Gateway:
    """Hook-chain orchestrator for agent + tool execution.

    The constructor takes an `AuditSink` so the audit hook (PLAN-015)
    can reach the sink via closure capture. PLAN-009 itself uses
    `_StubAuditSink` — swap at PLAN-015 Day 1.
    """

    def __init__(self, audit_sink: AuditSink | None = None) -> None:
        self._registry = HookRegistry()
        # default_audit_sink — PLAN-015 will replace at app startup.
        self.audit_sink: AuditSink = audit_sink or _StubAuditSink()

    # ----- Hook registration -------------------------------------------------

    def before_run(self, hook: BeforeRunHook, *, priority: int) -> None:
        """Register a `before_run` hook.

        `priority` is keyword-only (PLAN-009 §Day 3 amend) so callers
        can't accidentally bind a hook to a positional priority and
        flip the chain order.
        """
        self._registry.register_before(hook, priority=priority)

    def after_run(self, hook: AfterRunHook, *, priority: int) -> None:
        """Register an `after_run` hook (keyword-only priority)."""
        self._registry.register_after(hook, priority=priority)

    # ----- Inspection (test-friendly) ---------------------------------------

    def before_hooks(self) -> tuple[BeforeRunHook, ...]:
        return self._registry.before_hooks()

    def after_hooks(self) -> tuple[AfterRunHook, ...]:
        return self._registry.after_hooks()

    # ----- Execution ---------------------------------------------------------

    def _make_context(
        self,
        *,
        user_context: UserContext,
        agent_id: UUID | None,
        tool_name: str | None,
        question: str | None,
        request_id: UUID | None,
    ) -> RunContext:
        """Construct the immutable RunContext for one run."""
        return RunContext(
            user_context=user_context,
            agent_id=agent_id,
            request_id=request_id or uuid4(),
            tool_name=tool_name,
            question=question,
        )

    async def run(
        self,
        agent: Agent[Any, Any],
        *,
        deps: Any,
        user_context: UserContext,
        question: str,
        agent_id: UUID | None = None,
        request_id: UUID | None = None,
    ) -> Any:
        """Run an agent through the full hook chain.

        Steps:
          1. Build a `RunContext` (one `request_id` per invocation).
          2. Execute every `before_run` hook in priority order.
          3. Call `agent.run(question, deps=deps)`.
          4. Execute every `after_run` hook in priority order, passing
             the agent's output (Pydantic model).
          5. Return the agent's output (the pydantic-ai `AgentRunResult`'s
             `.output` field, unwrapped for caller convenience).

        Any hook exception propagates. Tool exceptions also propagate —
        after_run hooks **do not run** on tool failure (PLAN-014 audit
        hook reads exceptions via a separate error handler, not via
        the success path).
        """
        ctx = self._make_context(
            user_context=user_context,
            agent_id=agent_id,
            tool_name=None,
            question=question,
            request_id=request_id,
        )

        for before in self._registry.before_hooks():
            await before(ctx)

        result = await agent.run(question, deps=deps)
        output = result.output

        for after in self._registry.after_hooks():
            await after(ctx, output)

        return output


__all__ = ["Gateway"]
