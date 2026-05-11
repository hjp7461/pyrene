"""Verifies `DBAuditSink` satisfies `pyrene_core.audit.AuditSink`.

PLAN-015 Day 1 — structural typing check at mypy + runtime layers.

- Static: `sink: AuditSink = DBAuditSink(...)` would fail mypy if the
  shape drifted; the assignment in the test runs to keep the codepath
  exercised.
- Runtime: `@runtime_checkable` Protocol → `isinstance(...)` returns
  True without monkey-patching.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pyrene_audit import DBAuditSink
from pyrene_core import AuditSink


def test_db_audit_sink_is_audit_sink_runtime() -> None:
    """`DBAuditSink` matches `AuditSink` Protocol via structural typing."""
    sink = DBAuditSink(session_factory=MagicMock())
    assert isinstance(sink, AuditSink)


def test_db_audit_sink_assignment_static() -> None:
    """`sink: AuditSink = DBAuditSink(...)` — type-narrowing check."""
    sink: AuditSink = DBAuditSink(session_factory=MagicMock())
    assert sink is not None
