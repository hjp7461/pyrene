"""`POST/GET /gateway/servers` — admin-only MCP server registration.

PLAN-009 Day 4. Read endpoints are accessible to admin/analyst (the
catalog is informational); the mutating endpoints (`POST`, `discover`)
are admin-only per PRD-009 §2.1.

Tool discovery is exposed as a separate endpoint
(`POST /gateway/servers/{id}/discover`) so admins can re-sync without
re-creating the server row. The handler injects a `StdioMcpClient` via
a module-level factory hook so tests substitute a fake client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.dependencies import (
    _session_proxy,
    require_admin,
    require_any_role,
)
from pyrene_core import UserContext
from pyrene_gateway.mcp_client import StdioMcpClient
from pyrene_gateway.models import MCPServer
from pyrene_gateway.repository import (
    create_server,
    get_server_for_team,
    list_servers_for_team,
    list_tools_for_server,
)
from pyrene_gateway.schemas import (
    MCPServerCreate,
    MCPServerResponse,
    MCPToolResponse,
)
from pyrene_gateway.tool_discovery import discover_tools

# Test hook: routes inject `_client_factory(server)` for discovery. Tests
# override at module level (no FastAPI dependency override needed because
# the factory is not a request-scoped dep).
ClientFactory = Callable[[MCPServer], Awaitable[StdioMcpClient]]


async def _default_client_factory(server: MCPServer) -> StdioMcpClient:
    if server.command is None:
        raise ValueError(
            f"server {server.name!r} has transport=stdio but no command"
        )
    client = StdioMcpClient(
        command=server.command,
        args=tuple(server.args),
        env=dict(server.env),
    )
    await client.start()
    return client


_client_factory: ClientFactory = _default_client_factory


def set_client_factory(factory: ClientFactory) -> None:
    """Override the client factory (test seam). Returns the previous value
    so the test can restore."""
    global _client_factory
    _client_factory = factory


def reset_client_factory() -> None:
    global _client_factory
    _client_factory = _default_client_factory


servers_router = APIRouter(prefix="/gateway/servers", tags=["gateway"])

_require_reader = require_any_role("admin", "analyst")


def _to_response(server: MCPServer) -> MCPServerResponse:
    return MCPServerResponse(
        id=server.id,
        team_id=server.team_id,
        name=server.name,
        transport=server.transport,  # type: ignore[arg-type]
        command=server.command,
        args=tuple(server.args),
        env=dict(server.env),
        enabled=server.enabled,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


@servers_router.get("")
async def list_servers(
    current: Annotated[UserContext, Depends(_require_reader)],
    session: AsyncSession = Depends(_session_proxy),
) -> list[MCPServerResponse]:
    rows = await list_servers_for_team(session, current.team_id)
    return [_to_response(r) for r in rows]


@servers_router.post("", status_code=status.HTTP_201_CREATED)
async def create_server_endpoint(
    body: MCPServerCreate,
    current: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> MCPServerResponse:
    if body.transport == "stdio" and not body.command:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="transport=stdio requires command",
        )
    server = await create_server(
        session,
        team_id=current.team_id,
        name=body.name,
        transport=body.transport,
        command=body.command,
        args=body.args,
        env=body.env,
        enabled=body.enabled,
    )
    await session.commit()
    await session.refresh(server)
    return _to_response(server)


@servers_router.post("/{server_id}/discover")
async def discover_endpoint(
    server_id: UUID,
    current: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> list[MCPToolResponse]:
    server = await get_server_for_team(session, server_id, current.team_id)
    if server is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="mcp server not found"
        )
    if server.transport != "stdio":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"discover supports stdio only; got {server.transport!r}",
        )

    client = await _client_factory(server)
    try:
        tools = await discover_tools(session, server, client=client)
    finally:
        await client.stop()
    await session.commit()
    return [
        MCPToolResponse(
            id=t.id,
            server_id=t.server_id,
            name=t.name,
            description=t.description,
            input_schema=dict(t.input_schema),
            discovered_at=t.discovered_at,
        )
        for t in tools
    ]


@servers_router.get("/{server_id}/tools")
async def list_server_tools_endpoint(
    server_id: UUID,
    current: Annotated[UserContext, Depends(_require_reader)],
    session: AsyncSession = Depends(_session_proxy),
) -> list[MCPToolResponse]:
    server = await get_server_for_team(session, server_id, current.team_id)
    if server is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="mcp server not found"
        )
    rows = await list_tools_for_server(session, server.id)
    return [
        MCPToolResponse(
            id=t.id,
            server_id=t.server_id,
            name=t.name,
            description=t.description,
            input_schema=dict(t.input_schema),
            discovered_at=t.discovered_at,
        )
        for t in rows
    ]


__all__ = [
    "ClientFactory",
    "reset_client_factory",
    "servers_router",
    "set_client_factory",
]
