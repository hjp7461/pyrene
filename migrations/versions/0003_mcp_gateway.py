"""0003 mcp gateway: mcp_servers / mcp_tools.

Revision ID: 0003_mcp_gateway
Revises: 0002_agent_registry
Create Date: 2026-05-11

PLAN-009 Day 1. ADR-013 (b) FK cascade matrix applied:
  - `mcp_servers.team_id` ON DELETE CASCADE (team closure cleans up
    every server registration; the team owns the resource).
  - `mcp_tools.server_id` ON DELETE CASCADE (catalog rows follow the
    parent server — re-discovery rewrites them anyway).

Indexes:
  - `ix_mcp_servers_team_id` for the team-scoped list query.
  - `ix_mcp_servers_name`    for shortname lookups in the gateway router.
  - `ix_mcp_tools_server_id` for the per-server catalog dump.
  - `ix_mcp_tools_name`      for cross-server tool name resolution
    (PRD-009 §7 L-02: flat namespace; collision detection on `mcp_tools`).

UNIQUE constraints:
  - `uq_mcp_servers_team_name`  : `(team_id, name)` — per-team shortname.
  - `uq_mcp_tools_server_name`  : `(server_id, name)` — per-server tool.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_mcp_gateway"
down_revision: str | None = "0002_agent_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "teams.id", ondelete="CASCADE", name="fk_mcp_servers_team_id"
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("command", sa.String(length=2048), nullable=True),
        sa.Column(
            "args",
            postgresql.ARRAY(sa.String(length=2048)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "env",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
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
        sa.UniqueConstraint("team_id", "name", name="uq_mcp_servers_team_name"),
    )
    op.create_index("ix_mcp_servers_team_id", "mcp_servers", ["team_id"])
    op.create_index("ix_mcp_servers_name", "mcp_servers", ["name"])

    op.create_table(
        "mcp_tools",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "mcp_servers.id",
                ondelete="CASCADE",
                name="fk_mcp_tools_server_id",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "description", sa.String(length=2048), nullable=False, server_default=""
        ),
        sa.Column(
            "input_schema",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "server_id", "name", name="uq_mcp_tools_server_name"
        ),
    )
    op.create_index("ix_mcp_tools_server_id", "mcp_tools", ["server_id"])
    op.create_index("ix_mcp_tools_name", "mcp_tools", ["name"])


def downgrade() -> None:
    op.drop_index("ix_mcp_tools_name", table_name="mcp_tools")
    op.drop_index("ix_mcp_tools_server_id", table_name="mcp_tools")
    op.drop_table("mcp_tools")
    op.drop_index("ix_mcp_servers_name", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_team_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
