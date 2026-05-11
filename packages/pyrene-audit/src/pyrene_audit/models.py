"""SQLAlchemy 2.x async ORM model for PRD-015 (audit_events — WORM).

PLAN-015 Day 1. The on-disk shape mirrors `pyrene_core.audit.AuditEvent`
(the wire format used by every gateway hook) plus two chain-integrity
columns populated by the BEFORE INSERT trigger:

  - `prev_hash bytea NULL`   — direct predecessor's `row_hash` within
                                the same `team_id` chain (NULL on the
                                first row).
  - `row_hash  bytea NOT NULL` — sha256 of `coalesce(prev_hash, \\x00) ||
                                  canonical_json(row without row_hash)`.

Why per-team chains (not a single global chain):
  - Multi-tenant isolation: one team's audit export does not leak
    another team's row hashes as dependency context.
  - Reduces contention: per-team INSERT only blocks within that team.
  - Migration safety: a corrupted row breaks one team's chain, not all.

FK cascade policy (ADR-013 (b)):
  - `audit_events.user_id`  → `users(id)` ON DELETE RESTRICT — the WORM
    table is the canonical record of who did what; user soft-delete
    only (`users.deleted_at`).
  - `audit_events.team_id`  → `teams(id)` ON DELETE RESTRICT — chain
    integrity is keyed on `team_id`; team hard-delete would orphan the
    chain re-verification path.

Both FKs are nullable because some events have no owning user/team:
auth-failed on unknown email (`user_id` unknown), system bootstrap
events, etc.

Index policy (PLAN-015 §Day 1):
  - `(user_id, created_at desc)`  — admin user-history query.
  - `(team_id, created_at desc, id desc)` — chain tip lookup
    (BEFORE INSERT trigger hot path).
  - `(event_type, created_at desc)` — admin filter by event kind.
  - `(request_id)` — single-request trace stitch.
  - `(agent_id, created_at desc)` — per-agent regression evals.
  - BRIN on `created_at`   — large-table time-window scans.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Reuse the auth-side MetaData so cross-package FKs to `users` / `teams`
# resolve at the ORM layer. Mirrors `pyrene_agents.models` /
# `pyrene_gateway.models` (ADR-013 (a)).
from pyrene_auth.models import metadata as _shared_metadata


def _now_utc() -> datetime:
    """Module-level default factory — keeps mypy --strict happy."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for pyrene-audit.

    Shares `MetaData` with `pyrene_auth.models.Base` so `audit_events.user_id`
    → `users.id` resolves at the ORM layer. Alembic combines metadata in
    `migrations/env.py` (ADR-013 (a)).
    """

    metadata = _shared_metadata


metadata = Base.metadata


class AuditEventRow(Base):
    """WORM audit row.

    Application code MUST NOT set `prev_hash` / `row_hash` — the BEFORE
    INSERT trigger overwrites them. They are non-Optional in the ORM only
    for SELECT loads; INSERT statements omit them entirely.

    The table is INSERT-only at the role layer (`REVOKE UPDATE, DELETE,
    TRUNCATE ON audit_events FROM PUBLIC, pyrene_app`) and at the trigger
    layer (`audit_worm_guard()` RAISEs on UPDATE/DELETE/TRUNCATE unless
    `current_setting('audit.bypass', true) = 'on'` — set only inside
    super-role migrations).
    """

    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_audit_events_user_id"),
        nullable=True,
    )
    team_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="RESTRICT", name="fk_audit_events_team_id"),
        nullable=True,
    )
    agent_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=True,
    )
    request_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=True,
    )
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    # Trigger-populated chain columns. Application code never sets them.
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    row_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        Index("ix_audit_events_user_created", "user_id", "created_at"),
        # Chain tip lookup index — BEFORE INSERT trigger hot path.
        Index(
            "ix_audit_events_team_chain_tip",
            "team_id",
            "created_at",
            "id",
        ),
        Index("ix_audit_events_event_type_created", "event_type", "created_at"),
        Index("ix_audit_events_request_id", "request_id"),
        Index("ix_audit_events_agent_created", "agent_id", "created_at"),
    )


__all__ = ["AuditEventRow", "Base", "metadata"]
