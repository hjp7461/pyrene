"""`_StubAuditSink` — no-op AuditSink used until PLAN-015 lands `DBAuditSink`.

PLAN-009 (gateway) wires this as `default_audit_sink` so the canonical
5-stage hook chain (budget-pre / tool-rbac / data-rbac / tool / audit /
budget-post) can be exercised end-to-end without a database. PLAN-015
will register a `DBAuditSink` and the gateway's `audit_sink` slot will
swap at startup.

Test-friendly affordances:
  - `emit_count` lets tests assert "the gateway called the audit hook
    exactly N times" without monkey-patching the Protocol.
  - `clear()` resets the counter between cases.

`@runtime_checkable` isinstance check (`isinstance(_StubAuditSink(),
AuditSink)`) verifies the structural typing contract at PLAN-009 Day 1.
"""

from __future__ import annotations

from pyrene_core.audit.events import AuditEvent


class _StubAuditSink:
    """No-op AuditSink. Satisfies `AuditSink` Protocol via structural typing.

    Underscore prefix marks the class as internal-by-convention — host
    apps and tests instantiate it; PLAN-015's `DBAuditSink` is the
    public replacement.
    """

    def __init__(self) -> None:
        self.emit_count = 0

    async def emit(self, event: AuditEvent) -> None:
        """Increment counter; discard the event."""
        self.emit_count += 1
        # Reference `event` to keep mypy --strict from complaining about
        # an unused parameter while still being a true no-op semantically.
        _ = event

    def clear(self) -> None:
        """Reset the counter (test convenience)."""
        self.emit_count = 0


__all__ = ["_StubAuditSink"]
