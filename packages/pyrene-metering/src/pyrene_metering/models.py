"""SQLAlchemy 2.x async ORM model for PRD-013 (UsageRecord).

Schema overview:
  - `usage_records`: one row per agent run (or retry attempt). Records
    token usage + computed cost for billing/aggregation. PLAN-003 retry
    counter `attempt_idx` is the second component of the idempotency
    key so the same `request_id` can legitimately have multiple rows
    (one per retry attempt).

FK cascade policy (ADR-013 (b)):
  - `usage_records.user_id` → `users(id)` ON DELETE RESTRICT
    (billing/accounting preservation — user soft-delete only, hard delete
    forbidden).
  - `usage_records.team_id` → `teams(id)` ON DELETE RESTRICT
    (team-scoped rollups must not vanish on team closure; explicit
    teardown procedure required).

Uniqueness:
  - `UNIQUE(request_id, attempt_idx)` — idempotency. The same physical
    attempt cannot be billed twice; concurrent INSERTs race against the
    constraint and exactly one wins.

Precision:
  - `cost_usd` is `Numeric(18, 8)` — sub-cent micro-cost representation
    (e.g. $0.00000125 per 1k tokens). Decimal in Python — float forbidden
    (L-02 in PRD-013 §7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Share auth's MetaData so cross-package FKs (users / teams) resolve at
# ORM flush time (same pattern as `pyrene_gateway.models`).
from pyrene_auth.models import metadata as _shared_metadata


def _now_utc() -> datetime:
    """Module-level default factory (mypy-strict friendly — see auth models)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the metering package.

    Reuses the auth metadata so `usage_records.user_id` → `users.id`
    resolves at the ORM layer. Alembic combines metadata in
    `migrations/env.py` (ADR-013 (a)).
    """

    metadata = _shared_metadata


metadata = Base.metadata


class UsageRecord(Base):
    """One agent-run cost row.

    The `(request_id, attempt_idx)` pair is the idempotency key:
      - `request_id` is the Gateway's per-run identifier (PLAN-009).
      - `attempt_idx` is PLAN-003's 0-indexed retry counter. The 3 attempts
        of a retried run produce rows with `attempt_idx` 0, 1, 2 — all
        sharing the same `request_id`. A re-INSERT for the same
        `(request_id, attempt_idx)` pair (concurrent race or PLAN-003
        re-emission bug) is rejected at the DB layer.

    `agent_id` is NULL-able because some Phase 2 entry points (raw model
    probes, eval harness) run without an agent record. The aggregation
    API treats NULL as a separate bucket.

    `cache_read_tokens` / `cache_write_tokens` map to Pydantic AI
    `RunUsage.cache_read_tokens` / `cache_write_tokens` (ADR-002 D4).
    Models without provider-side cache reporting leave them at 0.
    """

    __tablename__ = "usage_records"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    request_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=False,
    )
    attempt_idx: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_usage_records_user_id",
        ),
        nullable=False,
    )
    team_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "teams.id",
            ondelete="RESTRICT",
            name="fk_usage_records_team_id",
        ),
        nullable=False,
    )
    agent_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=True,
        default=None,
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint(
            "request_id", "attempt_idx", name="uq_usage_records_request_attempt"
        ),
        # Hot-path indexes (PRD-013 §3.1).
        Index("ix_usage_records_user_created", "user_id", "created_at"),
        Index("ix_usage_records_team_created", "team_id", "created_at"),
        Index("ix_usage_records_request", "request_id"),
        Index("ix_usage_records_agent_created", "agent_id", "created_at"),
        Index("ix_usage_records_model_created", "model", "created_at"),
    )


__all__ = ["Base", "UsageRecord", "metadata"]
