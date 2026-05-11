"""Unit tests for `pyrene_core.audit`.

Covers PLAN-009 Day 1 completion criteria:
  - `AuditSink` is `@runtime_checkable` and `_StubAuditSink` satisfies it
    via `isinstance(...)`.
  - `_StubAuditSink: AuditSink` assignment type-checks (mypy --strict)
    and exercises the structural-typing contract at runtime.
  - `AuditEvent` is frozen + rejects extra fields (StrictBaseModel
    inheritance carries forward).
  - `AuditEvent` outcome is closed Literal — non-listed values rejected.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from pyrene_core import AuditEvent, AuditSink, _StubAuditSink


def test_stub_audit_sink_satisfies_protocol_runtime() -> None:
    """`@runtime_checkable` allows isinstance assertion (PLAN-009 Day 1)."""
    stub = _StubAuditSink()
    assert isinstance(stub, AuditSink)


def test_audit_sink_assignment_static() -> None:
    """`_StubAuditSink: AuditSink` — mypy --strict structural typing.

    Runtime equivalent: the variable annotation forces type narrowing.
    If `_StubAuditSink` ever drops `emit`, mypy fails this line; this
    test runs to keep the assignment in the codebase for that check.
    """
    sink: AuditSink = _StubAuditSink()
    assert sink is not None


async def test_stub_emit_counts() -> None:
    sink = _StubAuditSink()
    assert sink.emit_count == 0
    event = AuditEvent(event_type="tool_call", outcome="allowed")
    await sink.emit(event)
    await sink.emit(event)
    assert sink.emit_count == 2
    sink.clear()
    assert sink.emit_count == 0


def test_audit_event_frozen() -> None:
    """AuditEvent inherits StrictBaseModel — frozen + extra=forbid."""
    event = AuditEvent(event_type="tool_call", outcome="allowed")
    with pytest.raises(ValidationError):
        event.event_type = "mutated"


def test_audit_event_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(  # type: ignore[call-arg]
            event_type="tool_call",
            outcome="allowed",
            unknown_field="x",
        )


def test_audit_event_outcome_closed_literal() -> None:
    """`outcome` is `Literal["allowed", "denied", "error"]`."""
    AuditEvent(event_type="x", outcome="allowed")
    AuditEvent(event_type="x", outcome="denied")
    AuditEvent(event_type="x", outcome="error")
    with pytest.raises(ValidationError):
        AuditEvent(event_type="x", outcome="success")  # type: ignore[arg-type]


def test_audit_event_optional_uuid_fields() -> None:
    """All UUID fields default to None; explicit values pass through."""
    uid = uuid4()
    tid = uuid4()
    aid = uuid4()
    rid = uuid4()
    event = AuditEvent(
        event_type="tool_call",
        outcome="allowed",
        user_id=uid,
        team_id=tid,
        agent_id=aid,
        request_id=rid,
        tool_name="run_select",
        metadata={"row_count": 12},
    )
    assert event.user_id == uid
    assert event.team_id == tid
    assert event.agent_id == aid
    assert event.request_id == rid
    assert event.tool_name == "run_select"
    assert event.metadata == {"row_count": 12}


def test_audit_event_defaults_populate_id_and_timestamp() -> None:
    event = AuditEvent(event_type="x", outcome="allowed")
    assert event.id is not None
    assert event.created_at is not None


def test_audit_sink_non_emit_object_is_not_instance() -> None:
    """Negative: a plain class lacking `emit` is not an AuditSink instance."""

    class NotASink:
        pass

    assert not isinstance(NotASink(), AuditSink)
