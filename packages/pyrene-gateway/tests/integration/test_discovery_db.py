"""Integration: tool discovery upserts into `mcp_tools` (stubbed client).

PLAN-009 Day 4. Spawns the real Postgres via testcontainers, exercises
the discovery → UPSERT path with a fake `StdioMcpClient` so we do not
need a real MCP echo subprocess in CI. The real subprocess test is
covered by the unit tests against the SDK boundary.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.models import Team
from pyrene_gateway.mcp_client import DiscoveredTool
from pyrene_gateway.models import MCPServer, MCPTool
from pyrene_gateway.tool_discovery import discover_tools

pytestmark = pytest.mark.integration


class _FakeStdioMcpClient:
    """Stand-in for `StdioMcpClient` — implements the duck-typed surface
    that `tool_discovery.discover_tools` consumes."""

    def __init__(self, tools: tuple[DiscoveredTool, ...]) -> None:
        self._tools = tools

    async def start(self) -> None:  # pragma: no cover — owned_client=False path
        return

    async def stop(self) -> None:  # pragma: no cover — owned_client=False path
        return

    async def list_tools(self) -> tuple[DiscoveredTool, ...]:
        return self._tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        return {"echoed": (name, args)}


async def _seed_team_and_server(db_session: AsyncSession) -> MCPServer:
    team = Team(name=f"team-{uuid4().hex[:8]}")
    db_session.add(team)
    await db_session.flush()
    server = MCPServer(
        team_id=team.id,
        name="echo",
        transport="stdio",
        command="/usr/bin/echo",
        args=["hello"],
        env={},
    )
    db_session.add(server)
    await db_session.flush()
    return server


async def test_discovery_upserts_tools(db_session: AsyncSession) -> None:
    server = await _seed_team_and_server(db_session)
    fake = _FakeStdioMcpClient(
        tools=(
            DiscoveredTool(
                name="echo",
                description="echoes input",
                input_schema={"type": "object"},
            ),
            DiscoveredTool(name="ping", description="", input_schema={}),
        ),
    )

    tools = await discover_tools(db_session, server, client=fake)  # type: ignore[arg-type]
    names = sorted(t.name for t in tools)
    assert names == ["echo", "ping"]

    # Round-trip via SELECT.
    rows = await db_session.execute(
        select(MCPTool).where(MCPTool.server_id == server.id)
    )
    persisted_names = sorted(r.name for r in rows.scalars().all())
    assert persisted_names == ["echo", "ping"]


async def test_discovery_drops_stale_tools(db_session: AsyncSession) -> None:
    """Tools no longer reported by list_tools() are deleted from `mcp_tools`."""
    server = await _seed_team_and_server(db_session)
    first = _FakeStdioMcpClient(
        tools=(
            DiscoveredTool(name="a", description="", input_schema={}),
            DiscoveredTool(name="b", description="", input_schema={}),
        ),
    )
    await discover_tools(db_session, server, client=first)  # type: ignore[arg-type]

    # Second sync drops 'b'.
    second = _FakeStdioMcpClient(
        tools=(DiscoveredTool(name="a", description="updated", input_schema={"v": 2}),),
    )
    await discover_tools(db_session, server, client=second)  # type: ignore[arg-type]

    rows = await db_session.execute(
        select(MCPTool).where(MCPTool.server_id == server.id)
    )
    persisted = list(rows.scalars().all())
    assert {t.name for t in persisted} == {"a"}
    assert persisted[0].description == "updated"
    assert persisted[0].input_schema == {"v": 2}


async def test_discovery_rejects_non_stdio(db_session: AsyncSession) -> None:
    team = Team(name=f"team-{uuid4().hex[:8]}")
    db_session.add(team)
    await db_session.flush()
    server = MCPServer(
        team_id=team.id,
        name="sse-only",
        transport="sse",
        command="https://example.test/sse",
        args=[],
        env={},
    )
    db_session.add(server)
    await db_session.flush()

    with pytest.raises(ValueError, match="stdio"):
        await discover_tools(db_session, server)
