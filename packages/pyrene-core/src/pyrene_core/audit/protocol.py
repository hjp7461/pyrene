"""AuditSink Protocol — pluggable sink for audit events.

`@runtime_checkable` so PLAN-009 Day 1 can `isinstance(stub, AuditSink)`
to assert that the stub (and later PLAN-015's `DBAuditSink`) satisfies
the contract without a custom test runner.

Why a Protocol (not ABC):
  - `pyrene-gateway` (PLAN-009) should not import `pyrene-audit`
    (PLAN-015 — does not exist yet).
  - PLAN-015 wants to register its `DBAuditSink` against a contract that
    already ships with the gateway.
  - Pydantic AI / FastAPI ecosystem prefers Protocol for structural
    typing — keeps the dependency graph one-way (core → packages).

Contract:
  `async def emit(self, event: AuditEvent) -> None`
  - MUST NOT raise to the caller. Sinks log + swallow internal errors;
    the gateway's audit hook (priority 80) treats sink failure as
    `outcome="error"` on the secondary log, not as a fatal error for
    the original request. (PLAN-015 will own this detail; PLAN-009's
    stub simply does nothing.)
  - SHOULD be fast. Slow sinks block the after_run hook chain. PLAN-015
    will buffer + flush on a background task.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pyrene_core.audit.events import AuditEvent


@runtime_checkable
class AuditSink(Protocol):
    """Pluggable destination for `AuditEvent` records.

    See module docstring. Implementations must accept an `AuditEvent`
    and return `None`; exceptions propagate to the emitter (the gateway
    audit hook MAY rewrap as a secondary error event).
    """

    async def emit(self, event: AuditEvent) -> None:
        """Persist (or otherwise consume) the audit record.

        The default `_StubAuditSink` no-ops; PLAN-015 `DBAuditSink`
        inserts into `audit_events` via the `app_pool` (ADR-013 (d)).
        """
        ...


__all__ = ["AuditSink"]
