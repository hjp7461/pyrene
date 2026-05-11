"""Audit subsystem — `AuditEvent` schema + `AuditSink` Protocol.

Defines the contract that PLAN-009 gateway hooks emit against and
PLAN-015 (`DBAuditSink`) consumes. The Protocol lives in `pyrene-core`
so neither gateway nor audit packages need to import each other.

Public surface:
  - `AuditEvent`     — frozen Pydantic record (PRD-015 schema).
  - `AuditSink`      — runtime-checkable Protocol; `async emit(event)`.
  - `_StubAuditSink` — no-op default used by PLAN-009 until PLAN-015.
"""

from pyrene_core.audit._stub import _StubAuditSink
from pyrene_core.audit.events import AuditEvent, AuditOutcome
from pyrene_core.audit.protocol import AuditSink

__all__ = [
    "AuditEvent",
    "AuditOutcome",
    "AuditSink",
    "_StubAuditSink",
]
