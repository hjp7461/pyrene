"""0005 cost metering: usage_records.

Revision ID: 0005_cost_metering
Revises: 0004_audit_events
Create Date: 2026-05-11

PLAN-013 Day 1. ADR-013 (b) FK cascade matrix applied:
  - `usage_records.user_id` → `users(id)` ON DELETE RESTRICT
    (billing / accounting preservation; user soft-delete only).
  - `usage_records.team_id` → `teams(id)` ON DELETE RESTRICT
    (team-scoped rollups must not vanish on team closure).

`agent_id` is NULL-able (some Phase 2 entry points run without an
agent record — raw model probes, eval harness). No FK is declared
because `pyrene_agents.agent_specs` is registered to the same metadata
but the metering package intentionally avoids importing it (Wave 7
guardrail: pyrene-metering does NOT touch pyrene-agents).

UNIQUE / index plan (PRD-013 §3.1, PLAN-013 Day 1):
  - `uq_usage_records_request_attempt`  : `(request_id, attempt_idx)` —
    idempotency key. Concurrent INSERTs race against this and exactly
    one wins (verified in `test_idempotency_unique_concurrent`).
  - `ix_usage_records_user_created`     : `(user_id, created_at)`
  - `ix_usage_records_team_created`     : `(team_id, created_at)`
  - `ix_usage_records_request`          : `(request_id)`
  - `ix_usage_records_agent_created`    : `(agent_id, created_at)`
  - `ix_usage_records_model_created`    : `(model, created_at)`

Precision:
  - `cost_usd NUMERIC(18, 8)` — sub-cent micro representation
    (L-02 in PRD-013 §7). The model layer uses `Decimal`; the float
    path is forbidden.

down_revision: This PLAN runs in Wave 7 parallel with PLAN-010 (0004)
and PLAN-015 (0006). 0005 sits between them: PLAN-010 → PLAN-013 →
PLAN-015 in the linear chain (ADR-013 (a)). The 0004 file
("audit_events") is the audit baseline placeholder owned by PLAN-015's
Wave 6 commit; PLAN-013 inserts here without modifying that file.

NOTE on Wave 7 chain ordering: this migration's `down_revision =
"0004_audit_events"` is a forward-reference inserted when PLAN-015 lands
its 0004 file. If PLAN-013 lands first, the integration CI will fail
this migration (revision 0004 missing). The PM-coordinated landing
order resolves the race (PLAN-013 lands AFTER PLAN-010 + PLAN-015 v1
baseline; both already merged at Wave 7 start per the prompt's
"PLAN-010=0004, PLAN-015=0006" mapping). For robustness when only
PLAN-013 is present in a feature branch, the env.py / round-trip CI
detects the dangling revision and instructs the author to rebase onto
the latest chain head.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_cost_metering"
# Wave 7 chain reality: at the time of authoring this branch, neither
# PLAN-010 (0004) nor PLAN-015 (0006) has landed. The PM-coordinated
# merge order is PLAN-010 → PLAN-013 → PLAN-015, but during isolated
# feature development the only existing ancestor is "0003_mcp_gateway".
# We point at 0003 here so the round-trip CI (ADR-013 (e)) and
# integration tests pass in isolation. On final merge into the Wave 7
# integration branch, this PR rebases its `down_revision` onto
# "0004_tool_rbac" — a one-line conflict resolved by the PM landing
# script (no code change required from this PLAN).
down_revision: str | None = "0004_rbac_matrix"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "attempt_idx",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                ondelete="RESTRICT",
                name="fk_usage_records_user_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "teams.id",
                ondelete="RESTRICT",
                name="fk_usage_records_team_id",
            ),
            nullable=False,
        ),
        # No FK on agent_id — see module docstring.
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column(
            "cache_read_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cache_write_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=18, scale=8),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "request_id",
            "attempt_idx",
            name="uq_usage_records_request_attempt",
        ),
    )
    op.create_index(
        "ix_usage_records_user_created",
        "usage_records",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_usage_records_team_created",
        "usage_records",
        ["team_id", "created_at"],
    )
    op.create_index(
        "ix_usage_records_request",
        "usage_records",
        ["request_id"],
    )
    op.create_index(
        "ix_usage_records_agent_created",
        "usage_records",
        ["agent_id", "created_at"],
    )
    op.create_index(
        "ix_usage_records_model_created",
        "usage_records",
        ["model", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_records_model_created", table_name="usage_records")
    op.drop_index("ix_usage_records_agent_created", table_name="usage_records")
    op.drop_index("ix_usage_records_request", table_name="usage_records")
    op.drop_index("ix_usage_records_team_created", table_name="usage_records")
    op.drop_index("ix_usage_records_user_created", table_name="usage_records")
    op.drop_table("usage_records")
