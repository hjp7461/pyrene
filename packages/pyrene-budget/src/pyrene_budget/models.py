"""SQLAlchemy 2.x async ORM model for PRD-014 (BudgetLimit).

PLAN-014 Day 1. Owns one table:
  - `budget_limits`: one row per (scope, scope_id, period). Pre-flight
    hook (priority 10) reads it; post-flight hook (priority 90) re-reads
    it and emits an audit event if the realized cost overran the
    projection.

### Uniqueness + lock-key parity (PLAN-014 §Day 1)

`UNIQUE(scope, scope_id, period)` is the same composite the advisory
lock keys on (`hashtextextended(scope||':'||scope_id::text||':'||period, 0)`).
The unique index also doubles as the lookup index — pre-flight reads
hit it directly.

### FK policy (ADR-013 (b))

`scope_id` is polymorphic (user_id when scope="user", team_id when
scope="team"). Postgres does not support polymorphic FKs so we validate
the reference application-side. The RESTRICT cascade matrix from
metering / audit (billing-preservation) does not apply here: a deleted
scope leaves a tombstone budget row, but pre-flight against a deleted
scope is unreachable (auth would 401 first).

### Precision

`limit_usd: NUMERIC(18, 8)` — same precision contract as
`usage_records.cost_usd` so subtraction (`remaining = limit - used`)
stays exact. Decimal in Python; float forbidden (mirrors PRD-013 L-02).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Share auth's MetaData so cross-package combined-metadata works in
# `migrations/env.py` (same pattern as metering / audit).
from pyrene_auth.models import metadata as _shared_metadata


def _now_utc() -> datetime:
    """Module-level default factory (mypy --strict friendly)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for pyrene-budget.

    Reuses the auth metadata so future joins to `users` / `teams`
    resolve at the ORM layer. Alembic combines metadata in
    `migrations/env.py` (ADR-013 (a)).
    """

    metadata = _shared_metadata


metadata = Base.metadata


class BudgetLimit(Base):
    """One configured spend cap per (scope, scope_id, period).

    The advisory-lock pre-flight hook keys on the same composite —
    parity between the unique index and the lock key is intentional and
    enforced by `pyrene_budget.repository.lock_key_for`.

    `scope` is `"user" | "team"`; `agent` scope is out-of-scope for
    Phase 2 (PRD-014 §3.2 vs §3.1 contradicts on agent — Wave 8 PM
    amend defers agent budgets to Phase 3).

    `period` is `"day" | "week" | "month"`. The realized-usage rollup
    reads from `usage_records` using `created_at >= period_start_utc`
    semantics (see `pyrene_metering.aggregation._period_start`).
    """

    __tablename__ = "budget_limits"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=False,
    )
    period: Mapped[str] = mapped_column(String(8), nullable=False)
    limit_usd: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now_utc,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now_utc,
        onupdate=_now_utc,
    )

    __table_args__ = (
        # UNIQUE(scope, scope_id, period) is *also* the lookup index;
        # the advisory-lock hash keys on the same three columns, so the
        # serializer is the unique constraint plus the in-TXN lock.
        UniqueConstraint(
            "scope",
            "scope_id",
            "period",
            name="uq_budget_limits_scope_period",
        ),
        # Explicit named index that mirrors the unique constraint.
        # Postgres reuses the same btree under the hood, but the named
        # index makes EXPLAIN output legible.
        Index(
            "ix_budget_limits_scope_period",
            "scope",
            "scope_id",
            "period",
        ),
    )


__all__ = ["Base", "BudgetLimit", "metadata"]
