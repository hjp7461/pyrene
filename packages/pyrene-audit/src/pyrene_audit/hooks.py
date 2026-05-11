"""`make_audit_hook` — gateway after_run hook factory.

PLAN-015 Day 2. Returns a hook that builds an `AuditEvent` from the
`RunContext` and pipes it into a `AuditSink`. Registered at
`PRIORITY_AUDIT = 80` so it runs after tool execution but before
budget-post-charge.

### Why a factory (closure capture)

The gateway hook chain has a single `(ctx, result)` signature
(`AfterRunHook`). Sinks are injected via closure so the hook does not
need a global registry. PLAN-015's startup wires the DB sink at app
boot; tests can wire the stub sink.

### Fail-closed semantics

`emit(...)` exceptions propagate out of the hook → out of
`Gateway.run(...)`. PRD-015 §F1: audit-write failure blocks the
request. The route layer in PLAN-009 maps the exception class to a 5xx
+ Logfire alarm.

### outcome inference

PLAN-015 Day 2 ships the success path (`outcome="allowed"`).
Permission-denied + budget-exceeded paths emit their own dedicated
event types from PLAN-010 / PLAN-014 hooks (those hooks raise to veto
the request — `audit_emit` only sees successful runs because after_run
hooks are skipped on tool failure). See PLAN-015 §Day 2 outcome matrix
for the deny + error variants (separate hook + error handler).
"""

from __future__ import annotations

from typing import Any

from pyrene_core.audit import AuditEvent, AuditSink
from pyrene_gateway.context import RunContext
from pyrene_gateway.hooks import AfterRunHook


def make_audit_hook(
    sink: AuditSink,
    *,
    event_type: str = "agent.run",
) -> AfterRunHook:
    """Build an after_run hook that emits one `AuditEvent` per successful run.

    The returned coroutine closes over `sink` so PLAN-015 startup can
    swap implementations without touching the gateway's hook registry.
    """

    async def _audit_after_run(ctx: RunContext, result: Any) -> None:
        # Result is the agent's typed output; we keep it sink-opaque
        # (metadata only carries a tiny summary — repr length capped to
        # avoid blowing up the JSONB column with huge structured outputs).
        result_repr = repr(result)
        if len(result_repr) > 512:
            result_repr = result_repr[:509] + "..."

        event = AuditEvent(
            event_type=event_type,
            outcome="allowed",
            user_id=ctx.user_context.user_id,
            team_id=ctx.user_context.team_id,
            agent_id=ctx.agent_id,
            request_id=ctx.request_id,
            tool_name=ctx.tool_name,
            metadata={"result_repr": result_repr},
        )
        # Fail-closed: emit exceptions propagate up the chain.
        await sink.emit(event)

    return _audit_after_run


__all__ = ["make_audit_hook"]
