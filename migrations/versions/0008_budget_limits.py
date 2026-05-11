"""0008 budget limits: budget_limits.

Revision ID: 0008_budget_limits
Revises: 0007_data_permissions
Create Date: 2026-05-11

PLAN-014 Day 1. Wave 8 parallel — PLAN-011 (postgres adapter) claims
0007 (`0007_data_permissions`), PLAN-012 (mcp diversity) claims its
own migration if any, and this PLAN claims 0008. The down_revision
points at 0007 to preserve a linear chain (ADR-013 (a)) once Wave 8
lands.

### Wave 8 chain reality

At the time of authoring this branch in isolation, none of the
Wave-8 sibling migrations have landed yet (the latest on disk is
`0006_audit_log`). The forward-reference `down_revision =
"0007_data_permissions"` reflects the PM-coordinated merge order
(PLAN-011 → PLAN-014). For an isolated feature branch the
round-trip CI (ADR-013 (e)) detects the dangling revision and the
PM landing script rewrites this single line on integration. No
code changes from this PLAN are required.

### Schema (PRD-014 §4)

  - `budget_limits`:
      - `id UUID PK`
      - `scope VARCHAR(16) NOT NULL`         (closed set: "user", "team")
      - `scope_id UUID NOT NULL`             (polymorphic — see below)
      - `period VARCHAR(8) NOT NULL`         (closed set: "day", "week", "month")
      - `limit_usd NUMERIC(18, 8) NOT NULL`
      - `created_at TIMESTAMPTZ NOT NULL`
      - `updated_at TIMESTAMPTZ NOT NULL`
  - `UNIQUE(scope, scope_id, period)` — composite uniqueness; matches
    the advisory-lock key derivation so the unique constraint and the
    in-TXN advisory lock together close PRD-014 §위험 신호 #1 race.
  - `(scope, scope_id, period)` btree index (explicit alias for EXPLAIN
    legibility; the unique constraint owns the same btree).

### FK policy (PRD-014 §4)

`scope_id` is polymorphic (user_id when scope='user', team_id when
scope='team'); Postgres has no native polymorphic FK. We validate the
reference application-side at upsert time and rely on auth's 401 to
gate pre-flight against deleted scope. The RESTRICT cascade matrix
from audit / metering does not apply.

### Precision

`limit_usd NUMERIC(18, 8)` — same precision contract as
`usage_records.cost_usd` so `Decimal(limit) - Decimal(used)` is exact.
Float-as-cents is forbidden (PRD-013 L-02 inheritance).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008_budget_limits"
# Wave 8 chain: PLAN-011 = 0007_data_permissions, PLAN-014 = 0008.
# See module docstring "Wave 8 chain reality" for the landing-order
# rewrite that the PM script applies on integration.
down_revision: str | None = "0007_data_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "budget_limits",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column(
            "scope_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column(
            "limit_usd",
            sa.Numeric(precision=18, scale=8),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "scope",
            "scope_id",
            "period",
            name="uq_budget_limits_scope_period",
        ),
    )
    op.create_index(
        "ix_budget_limits_scope_period",
        "budget_limits",
        ["scope", "scope_id", "period"],
    )


def downgrade() -> None:
    op.drop_index("ix_budget_limits_scope_period", table_name="budget_limits")
    op.drop_table("budget_limits")
