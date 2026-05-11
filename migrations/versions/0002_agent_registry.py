"""0002 agent registry: agent_specs / agent_versions.

Revision ID: 0002_agent_registry
Revises: 0001_auth
Create Date: 2026-05-11

PLAN-008 Day 1. ADR-013 (b) FK cascade matrix applied:
  - `agent_specs.team_id` ON DELETE CASCADE (team closure cleans up specs).
  - `agent_specs.created_by` ON DELETE RESTRICT (authorship preserved).
  - `agent_versions.agent_id` ON DELETE CASCADE (versions follow parent).
  - `agent_versions.created_by` ON DELETE RESTRICT (PRD-008 §3.2).

INSERT-only role policy:
  After creating `agent_versions`, this migration runs:
    REVOKE UPDATE, DELETE ON agent_versions FROM pyrene_app;
  to enforce immutability at the DB layer (defense-in-depth alongside the
  ORM `__table_args__` marker). The REVOKE is wrapped in a DO block so the
  migration is no-op in environments where `pyrene_app` doesn't exist
  (testcontainers / local dev that connect as superuser).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_agent_registry"
down_revision: str | None = "0001_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_specs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE", name="fk_agent_specs_team_id"),
            nullable=False,
        ),
        sa.Column(
            "description", sa.String(length=2048), nullable=False, server_default=""
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            # ADR-013 (b): RESTRICT preserves authorship across user lifecycle.
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name="fk_agent_specs_created_by"
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("team_id", "name", name="uq_agent_specs_team_name"),
    )
    op.create_index("ix_agent_specs_name", "agent_specs", ["name"])
    op.create_index("ix_agent_specs_team_id", "agent_specs", ["team_id"])

    op.create_table(
        "agent_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "agent_specs.id",
                ondelete="CASCADE",
                name="fk_agent_versions_agent_id",
            ),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("output_schema_key", sa.String(length=128), nullable=False),
        sa.Column("system_prompt", sa.String(length=16384), nullable=False),
        sa.Column(
            "tools",
            postgresql.ARRAY(sa.String(length=128)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            # ADR-013 (b): RESTRICT preserves authorship across user lifecycle.
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name="fk_agent_versions_created_by"
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "agent_id", "version", name="uq_agent_versions_agent_version"
        ),
    )
    op.create_index("ix_agent_versions_agent_id", "agent_versions", ["agent_id"])

    # INSERT-only role enforcement. PLAN-008 §Day 1 + ADR-013 (d).
    # Wrapped in DO so the migration is a no-op when `pyrene_app` doesn't
    # exist (testcontainers connect as superuser; the GRANT layer is set up
    # by deploy scripts).
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pyrene_app') THEN
            EXECUTE 'REVOKE UPDATE, DELETE ON agent_versions FROM pyrene_app';
            EXECUTE 'GRANT INSERT, SELECT ON agent_versions TO pyrene_app';
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_agent_versions_agent_id", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_index("ix_agent_specs_team_id", table_name="agent_specs")
    op.drop_index("ix_agent_specs_name", table_name="agent_specs")
    op.drop_table("agent_specs")
