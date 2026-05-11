"""SQLAlchemy 2.x async ORM models for PRD-009 (MCPServer + MCPTool).

Schema overview:
  - `mcp_servers`: per-team registered MCP server endpoint.
    Phase 2 supports `stdio` (subprocess) and `sse` (HTTP-over-SSE)
    transports. `command` + `args` are required when `transport=stdio`;
    PRD-009 §7 L-02 reserves the namespace policy for Day 4.
  - `mcp_tools`: discovered tool catalog (one row per MCP `tools/list`
    entry). `input_schema` is the raw JSON Schema from the MCP server;
    the gateway hashes it for cache invalidation on re-sync.

FK cascade policy (ADR-013 (b)):
  - `mcp_servers.team_id` → `teams(id)` ON DELETE CASCADE
    (server registration is team-scoped; team closure cleans up).
  - `mcp_tools.server_id` → `mcp_servers(id)` ON DELETE CASCADE
    (tool catalog follows its parent server).

Uniqueness:
  - `UNIQUE(team_id, name)` on `mcp_servers` — a team cannot register
    two MCP servers under the same shortname.
  - `UNIQUE(server_id, name)` on `mcp_tools` — each server's tool
    namespace is exhaustive (no duplicate tool names within a server).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Reuse the auth-side MetaData so cross-package FKs resolve at ORM flush
# (mirrors `pyrene_agents.models` pattern — see ADR-013 (a) + the
# pyrene_agents docstring for the rationale).
from pyrene_auth.models import metadata as _shared_metadata


def _now_utc() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the gateway package.

    Shares MetaData with `pyrene_auth.models.Base` so `mcp_servers.team_id`
    → `teams.id` resolves at the ORM layer. Alembic combines metadata in
    `migrations/env.py` (ADR-013 (a)).
    """

    metadata = _shared_metadata


metadata = Base.metadata


class MCPServer(Base):
    """Registered MCP server (Phase 2: per-team scoped).

    `env` is a JSONB blob of environment variables passed to the
    subprocess (`stdio`) or used as HTTP headers (`sse`). Phase 2.5 will
    encrypt sensitive values at rest; Phase 2 ships plaintext but
    relies on DB-side `pg_dump` ACLs (ADR-013 (d) — `app_pool` role).

    `transport`:
      - `stdio`: command + args spawn a subprocess; messages via stdin/stdout.
      - `sse`:   server-sent events over HTTP; `command` field carries
                 the base URL, `args` is unused.
    """

    __tablename__ = "mcp_servers"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    team_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_mcp_servers_team_id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    command: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    args: Mapped[list[str]] = mapped_column(
        ARRAY(String(2048)), nullable=False, default=list
    )
    # JSONB carries the env map; Pydantic schema layer enforces str→str.
    env: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now_utc,
        onupdate=_now_utc,
    )

    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_mcp_servers_team_name"),
    )


class MCPTool(Base):
    """Tool catalog row, populated by `discover_tools(server)`.

    `input_schema` is the raw JSON Schema from MCP `tools/list`. The
    gateway treats it as opaque at the model layer; the Pydantic
    schema wrapper validates it before persisting.
    """

    __tablename__ = "mcp_tools"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    server_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "mcp_servers.id", ondelete="CASCADE", name="fk_mcp_tools_server_id"
        ),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    input_schema: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("server_id", "name", name="uq_mcp_tools_server_name"),
    )


__all__ = ["Base", "MCPServer", "MCPTool", "metadata"]
