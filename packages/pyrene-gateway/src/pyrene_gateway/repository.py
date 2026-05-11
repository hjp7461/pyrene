"""Async SQLAlchemy repository helpers for MCPServer + MCPTool.

PLAN-009 Day 4. Keeps SQL out of routes so test seams stay narrow.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_gateway.models import MCPServer, MCPTool


async def list_servers_for_team(
    session: AsyncSession, team_id: UUID
) -> Sequence[MCPServer]:
    """Return every MCPServer registered to `team_id`, ordered by name."""
    result = await session.execute(
        select(MCPServer)
        .where(MCPServer.team_id == team_id)
        .order_by(MCPServer.name)
    )
    return result.scalars().all()


async def get_server_for_team(
    session: AsyncSession, server_id: UUID, team_id: UUID
) -> MCPServer | None:
    """Fetch one server scoped to `team_id` (404-vs-403 defense in routes)."""
    result = await session.execute(
        select(MCPServer)
        .where(MCPServer.id == server_id)
        .where(MCPServer.team_id == team_id)
    )
    return result.scalar_one_or_none()


async def create_server(
    session: AsyncSession,
    *,
    team_id: UUID,
    name: str,
    transport: str,
    command: str | None,
    args: tuple[str, ...],
    env: dict[str, str],
    enabled: bool,
) -> MCPServer:
    """Insert a new MCPServer row. Caller commits."""
    server = MCPServer(
        team_id=team_id,
        name=name,
        transport=transport,
        command=command,
        args=list(args),
        env=env,
        enabled=enabled,
    )
    session.add(server)
    await session.flush()
    return server


async def list_tools_for_server(
    session: AsyncSession, server_id: UUID
) -> Sequence[MCPTool]:
    """Return the discovered tool catalog for `server_id`."""
    result = await session.execute(
        select(MCPTool).where(MCPTool.server_id == server_id).order_by(MCPTool.name)
    )
    return result.scalars().all()


__all__ = [
    "create_server",
    "get_server_for_team",
    "list_servers_for_team",
    "list_tools_for_server",
]
