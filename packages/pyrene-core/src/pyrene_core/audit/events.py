"""AuditEvent — the canonical record emitted by every audited operation.

Phase 2 hot path (PRD-009 / PLAN-015): Gateway hooks (PLAN-009 Day 3) emit
an `AuditEvent` per tool call so PRD-015 (audit log) can persist them via
an `AuditSink` implementation. The event is provider-neutral — the stub
(`_StubAuditSink`) and the future DB-backed sink (`DBAuditSink`) both
accept the same payload.

Why this lives in `pyrene-core`:
  - PLAN-009 (gateway) emits events.
  - PLAN-010 (tool RBAC) emits deny events.
  - PLAN-013 / PLAN-014 (cost / budget) emit budget-exceeded events.
  - PLAN-015 (audit) consumes them.
  Putting `AuditEvent` + `AuditSink` Protocol in `pyrene-core` avoids
  circular dependency between gateway and audit packages — every layer
  imports the contract from the same root.

Schema notes:
  - `StrictBaseModel` (frozen, extra=forbid). Audit records are
    immutable by contract (F-06 WORM); freezing the Pydantic model at
    the application boundary matches the DB-side INSERT-only role.
  - `outcome` is a closed `Literal` — extending it is a schema migration,
    not a free-text proliferation.
  - `metadata` is a sink-opaque dict for sink-specific context (e.g.
    PLAN-014 budget_remaining, PLAN-010 deny_reason). Sinks are free to
    persist or drop the field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field

from pyrene_core.models import StrictBaseModel

AuditOutcome = Literal["allowed", "denied", "error"]
"""Closed set of audit outcomes.

- `allowed`: request passed all gates and (if applicable) succeeded.
- `denied`: a policy gate (RBAC / budget) blocked the request.
- `error`: an unexpected runtime failure (tool exception, transport error).
"""


def _now_utc() -> datetime:
    """Module-level default factory so Pydantic doesn't bind a lambda."""
    return datetime.now(UTC)


class AuditEvent(StrictBaseModel):
    """Single audit record. Frozen, validated, sink-agnostic.

    `id` is generated client-side (UUIDv4) so the emitting hook can stamp
    the same id onto the Logfire span — joining traces to audit rows
    becomes a lookup, not a probabilistic correlation.

    `request_id` joins multi-step traces (a single agent run that fans
    out to several tool calls shares one request_id). `agent_id` is the
    AgentSpec id (PLAN-008). `team_id` is the partition key for tenant
    isolation (PLAN-013/015 cost rollups).

    `tool_name` is nullable because some events (auth-only, budget-only)
    are not tied to a specific tool. When set, it matches the value the
    gateway resolved against the tool registry.
    """

    id: UUID = Field(default_factory=uuid4)
    event_type: str
    user_id: UUID | None = None
    team_id: UUID | None = None
    agent_id: UUID | None = None
    request_id: UUID | None = None
    tool_name: str | None = None
    outcome: AuditOutcome
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now_utc)


__all__ = ["AuditEvent", "AuditOutcome"]
