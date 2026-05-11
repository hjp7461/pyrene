"""Tool discovery — register / refresh `MCPTool` rows from a running server.

PLAN-009 Day 2. Given an `MCPServer` row, spawn the corresponding stdio
client (Day 2's `StdioMcpClient`), call `list_tools()`, and UPSERT the
catalog into `mcp_tools`.

UPSERT semantics:
  - Match by (`server_id`, `name`). Existing rows update
    `description`, `input_schema`, `discovered_at`.
  - Tools that exist in DB but not in the current `list_tools()` are
    DELETED — the server is the source of truth for its own catalog.

Phase 2 limitations:
  - Only `transport="stdio"` is supported in this function. PRD-009 §3
    lists SSE too; Phase 2.5 adds an `SseMcpClient` and a
    `discover_tools_sse` variant.
  - No retry on transient failure. PLAN-009 Day 3 health check retries
    every 60s by re-invoking `discover_tools`.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_gateway.mcp_client import DiscoveredTool, StdioMcpClient
from pyrene_gateway.models import MCPServer, MCPTool


async def discover_tools(
    session: AsyncSession,
    server: MCPServer,
    *,
    client: StdioMcpClient | None = None,
) -> tuple[MCPTool, ...]:
    """Refresh the `mcp_tools` catalog for `server`.

    `client` is an optional injection point for tests — production callers
    pass `None` and the function spawns a fresh `StdioMcpClient`. The
    `client` lifecycle is managed in this function (started/stopped) when
    it was created here; an injected client is left as-is.

    Returns the new catalog as ORM instances (already flushed, not committed).
    """
    if server.transport != "stdio":
        raise ValueError(
            f"discover_tools only supports stdio transport; got {server.transport!r}"
        )

    owned_client = client is None
    if client is None:
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

    try:
        discovered: tuple[DiscoveredTool, ...] = await client.list_tools()
    finally:
        if owned_client:
            await client.stop()

    await _upsert_tools(session, server, discovered)
    # Re-read fresh rows for the caller.
    rows = await session.execute(
        select(MCPTool).where(MCPTool.server_id == server.id)
    )
    return tuple(rows.scalars().all())


async def _upsert_tools(
    session: AsyncSession,
    server: MCPServer,
    discovered: Iterable[DiscoveredTool],
) -> None:
    """Upsert discovered tools + delete tools no longer reported."""
    seen_names: set[str] = set()
    for tool in discovered:
        seen_names.add(tool.name)
        stmt = insert(MCPTool).values(
            server_id=server.id,
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
        )
        # ON CONFLICT (server_id, name) DO UPDATE — keeps PK stable, refreshes
        # description / schema / discovered_at.
        stmt = stmt.on_conflict_do_update(
            constraint="uq_mcp_tools_server_name",
            set_={
                "description": stmt.excluded.description,
                "input_schema": stmt.excluded.input_schema,
                "discovered_at": func.now(),
            },
        )
        await session.execute(stmt)

    # Drop rows no longer in the catalog. The hot path uses
    # `name NOT IN (...)` which is fine for the typical 10-100 tool catalog
    # per server.
    if seen_names:
        await session.execute(
            delete(MCPTool)
            .where(MCPTool.server_id == server.id)
            .where(MCPTool.name.not_in(seen_names))
        )
    else:
        # Server reported zero tools — wipe the catalog wholesale.
        await session.execute(
            delete(MCPTool).where(MCPTool.server_id == server.id)
        )

    await session.flush()


__all__ = ["discover_tools"]
