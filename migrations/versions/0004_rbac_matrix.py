"""0004 rbac matrix: permissions.

Revision ID: 0004_rbac_matrix
Revises: 0003_mcp_gateway
Create Date: 2026-05-11

PLAN-010 Day 1. ADR-013 (b) FK cascade matrix applied:
  - `permissions.role_id` -> `roles.id` ON DELETE **RESTRICT**.
    Implementation note: this is the first table that pins a role
    in place. Role deletes now require explicit revocation of every
    permission row first — PRD-010 §5 "실수 권한 박탈 방지". The
    admin /admin/roles DELETE endpoint already maps IntegrityError
    to HTTP 409.

Indexes:
  - `ix_permissions_tool_role` on `(tool_name, role_id)` — RBAC
    check hot path. Tool name leads because the gateway always
    knows the tool before it knows the caller's roles.

UNIQUE:
  - `uq_permissions_role_tool_action` on `(role_id, tool_name, action)`
    — at most one allow row + one deny row per (role, tool) pair.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_rbac_matrix"
down_revision: str | None = "0003_mcp_gateway"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "permissions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "roles.id",
                # ADR-013 (b): RESTRICT — accidental role drop must
                # not silently strip privileges from every user
                # holding that role. Admin endpoint maps the
                # resulting IntegrityError to HTTP 409.
                ondelete="RESTRICT",
                name="fk_permissions_role_id",
            ),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "role_id", "tool_name", "action",
            name="uq_permissions_role_tool_action",
        ),
    )
    # Composite index: tool_name leads because the RBAC check filters
    # `WHERE tool_name = ? AND role_id IN (...)`.
    op.create_index(
        "ix_permissions_tool_role", "permissions", ["tool_name", "role_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_permissions_tool_role", table_name="permissions")
    op.drop_table("permissions")
