"""Pyrene audit log (PRD-015) — WORM table + per-team hash chain.

Phase 2 backbone:
  - `AuditEventRow` — SQLAlchemy 2.x model for `audit_events`.
  - `DBAuditSink`   — `AuditSink` Protocol implementation (DB-backed).
  - `make_audit_hook` — `AfterRunHook` factory for `PRIORITY_AUDIT = 80`.
  - `register_audit_sink` — startup helper that swaps the gateway's stub
    sink for the DB sink AND registers the after_run hook.
  - `audit_router` — `GET /audit/events` + `GET /audit/timeline`.

Dependency direction (ADR-013 (a)):
  pyrene-audit → pyrene-gateway, pyrene-auth, pyrene-core.
  No edge points back.
"""

from pyrene_audit.db_sink import DBAuditSink
from pyrene_audit.hooks import make_audit_hook
from pyrene_audit.models import AuditEventRow, Base, metadata
from pyrene_audit.routes import audit_router
from pyrene_audit.schemas import (
    AuditEventPage,
    AuditEventResponse,
    AuditTimelineBucket,
)
from pyrene_audit.startup import register_audit_sink

__version__ = "0.1.0"

__all__ = [
    "AuditEventPage",
    "AuditEventResponse",
    "AuditEventRow",
    "AuditTimelineBucket",
    "Base",
    "DBAuditSink",
    "audit_router",
    "make_audit_hook",
    "metadata",
    "register_audit_sink",
]
