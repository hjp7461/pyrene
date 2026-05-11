"""Pydantic DTOs for the MCP gateway: server registration + tool catalog.

Read-only DTOs (`*Response`) and write DTOs (`*Create`) follow the same
pattern as `pyrene-agents.schemas`:
  - frozen `StrictBaseModel` for in-memory passing.
  - explicit `from_orm`-style classmethods stay in the repository layer
    (`pyrene_gateway.repository`) so request models do not depend on
    SQLAlchemy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from pyrene_core import StrictBaseModel

MCPTransport = Literal["stdio", "sse"]
"""Closed set of transports (PRD-009 §3). `in-process` was deferred — the
Phase 1 SQL tools live in `pyrene-agents.tool_registry` and the gateway
calls them directly via the Pydantic AI tool decoration (no MCP framing
needed for in-process).
"""


class MCPServerCreate(StrictBaseModel):
    """Request body for `POST /gateway/servers`."""

    name: str = Field(min_length=1, max_length=128)
    transport: MCPTransport
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class MCPServerResponse(StrictBaseModel):
    """Response for server list / detail endpoints."""

    id: UUID
    team_id: UUID
    name: str
    transport: MCPTransport
    command: str | None
    args: tuple[str, ...]
    env: dict[str, str]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class MCPToolResponse(StrictBaseModel):
    """Discovered tool catalog row."""

    id: UUID
    server_id: UUID
    name: str
    description: str
    input_schema: dict[str, object]
    discovered_at: datetime


__all__ = [
    "MCPServerCreate",
    "MCPServerResponse",
    "MCPToolResponse",
    "MCPTransport",
]
