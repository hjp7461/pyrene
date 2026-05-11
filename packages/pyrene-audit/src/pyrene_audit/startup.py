"""Startup wiring — swap gateway's stub audit sink for `DBAuditSink`.

PLAN-015 Day 2 cross-PLAN handoff. The host application calls
`register_audit_sink(gateway, sink)` once at boot:

```python
from pyrene_audit import DBAuditSink, register_audit_sink

sink = DBAuditSink(session_factory)
register_audit_sink(gateway, sink)
```

This module owns the *only* place application code is allowed to
mutate `gateway.audit_sink`. Keeping the swap in `pyrene-audit` means
PLAN-009 (gateway) does not import PLAN-015 — the dependency arrow
points one way (audit → gateway), preserving the ADR-013 ordering.

We also register the `make_audit_hook(...)` after_run hook at
`PRIORITY_AUDIT = 80`. Callers receive the registered hook so tests
can introspect / un-register if needed.
"""

from __future__ import annotations

from pyrene_audit.hooks import make_audit_hook
from pyrene_core.audit import AuditSink
from pyrene_gateway import PRIORITY_AUDIT, Gateway
from pyrene_gateway.hooks import AfterRunHook


def register_audit_sink(
    gateway: Gateway,
    sink: AuditSink,
    *,
    event_type: str = "agent.run",
) -> AfterRunHook:
    """Swap `gateway.audit_sink` and register the after_run audit hook.

    Returns the registered hook so callers can verify the registration
    (`hook in gateway.after_hooks()`) — useful for test assertions.
    """
    # 1) Replace the stub. PLAN-009 exposes `audit_sink` as a public
    #    attribute precisely so PLAN-015 can swap it.
    gateway.audit_sink = sink

    # 2) Register the emit hook at the canonical priority.
    hook = make_audit_hook(sink, event_type=event_type)
    gateway.after_run(hook, priority=PRIORITY_AUDIT)
    return hook


__all__ = ["register_audit_sink"]
