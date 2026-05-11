"""Thin wrapper around the official `mcp` Python SDK for stdio transports.

PLAN-009 Day 2. The official SDK (`mcp.ClientSession` + `mcp.client.stdio.stdio_client`)
handles the JSON-RPC framing, message correlation, and process lifecycle.
We wrap it for three reasons:

1. **Lifecycle**: SDK's `stdio_client` is an async context manager. We expose
   `start()` / `stop()` so the gateway can hold a long-lived session per
   registered server (PRD-009 S4 — health check at 60s).
2. **Typing**: SDK returns `ListToolsResult` and `CallToolResult` types that
   leak `mcp.types` into every caller. We project to small dataclasses
   (`DiscoveredTool`) so downstream code only depends on `pyrene-gateway`.
3. **Mocking**: PLAN-009 Day 4 integration test runs without spawning a real
   subprocess. Wrapping behind a class lets us inject a fake session in
   tests via dependency injection.

### Timeout / restart policy (PRD-009 F1 / F2)

- `call_tool` wraps in `asyncio.wait_for(..., timeout=10)`. SDK already
  takes a `read_timeout_seconds` but our 10s ceiling is the contract.
- `start()` failure raises `McpStartupError`. The gateway maps it to the
  `unavailable` server state; PLAN-009 Day 3 health check retries every 60s.
- `stop()` is idempotent — calling on an already-stopped session is a no-op.

### Why no asyncpg/subprocess directly

The SDK already handles `stdio_client` cleanly via `anyio` task groups.
Re-implementing the subprocess + framing would duplicate ~400 LOC for
no gain. PRD-009 §7 L-01 explicitly preferred "official SDK".
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Self

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpStartupError(RuntimeError):
    """Raised when the stdio transport fails to start (subprocess spawn,
    handshake, etc.). Caller maps to PRD-009 F1 — server unavailable."""


class McpToolError(RuntimeError):
    """Raised when `call_tool` fails (timeout, JSON-RPC error, server error).

    Surfaces PRD-009 F2 to the caller; the gateway audit hook records the
    failure with `outcome="error"`.
    """


@dataclass(frozen=True)
class DiscoveredTool:
    """Minimal projection of `mcp.types.Tool` for the gateway models layer."""

    name: str
    description: str
    input_schema: dict[str, Any]


class StdioMcpClient:
    """Long-lived stdio MCP session manager.

    Usage:
        client = StdioMcpClient(command="echo", args=["hello"])
        await client.start()
        tools = await client.list_tools()
        result = await client.call_tool("echo", {"text": "hi"})
        await client.stop()

    Or, as an async context manager:
        async with StdioMcpClient(command="echo") as client:
            tools = await client.list_tools()

    The class is `not thread-safe`. Phase 2 is single-asyncio-loop; if
    Phase 3 introduces a per-server worker pool, switch to a per-task
    lock around `call_tool`.
    """

    DEFAULT_TIMEOUT = timedelta(seconds=10)

    def __init__(
        self,
        *,
        command: str,
        args: tuple[str, ...] | list[str] = (),
        env: dict[str, str] | None = None,
        timeout: timedelta = DEFAULT_TIMEOUT,
    ) -> None:
        self._params = StdioServerParameters(
            command=command,
            args=list(args),
            env=env,
        )
        self._timeout = timeout
        # The official SDK uses async context managers. We exit them on stop.
        # `_stack` holds the open contexts so they can be exited in reverse
        # order. We avoid `contextlib.AsyncExitStack` to keep error attribution
        # explicit in stack traces.
        self._session: ClientSession | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._started_event = asyncio.Event()
        self._stop_event = asyncio.Event()

    # --- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Spawn the subprocess + initialize an MCP ClientSession.

        Runs the SDK's `stdio_client` + `ClientSession` async context in a
        background task so the caller can keep a long-lived handle (the SDK
        is shaped around `async with`, but the gateway needs an explicit
        start/stop lifecycle).
        """
        if self._session is not None:
            return  # idempotent

        ready = asyncio.Event()
        startup_error: list[BaseException] = []

        async def _run() -> None:
            try:
                async with (
                    stdio_client(self._params) as (read, write),
                    ClientSession(
                        read,
                        write,
                        read_timeout_seconds=self._timeout,
                    ) as session,
                ):
                    await session.initialize()
                    self._session = session
                    ready.set()
                    await self._stop_event.wait()
            except BaseException as exc:
                # Any startup error (subprocess spawn, handshake, etc.) is
                # captured and surfaced via start() as McpStartupError.
                startup_error.append(exc)
                ready.set()

        self._stop_task = asyncio.create_task(_run(), name="mcp-stdio-session")
        await ready.wait()
        if startup_error:
            self._session = None
            raise McpStartupError(
                f"mcp stdio startup failed: {startup_error[0]!r}"
            ) from startup_error[0]
        self._started_event.set()

    async def stop(self) -> None:
        """Signal the background task to exit; wait for cleanup."""
        if self._stop_task is None:
            return  # idempotent
        self._stop_event.set()
        # The session task may have raised on exit; ignore — the goal is
        # to release the subprocess, not to bubble shutdown noise.
        with contextlib.suppress(BaseException):
            await self._stop_task
        self._session = None
        self._stop_task = None
        self._stop_event = asyncio.Event()  # reset for re-start
        self._started_event = asyncio.Event()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # --- RPC -----------------------------------------------------------------

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise McpStartupError("client not started; call start() first")
        return self._session

    async def list_tools(self) -> tuple[DiscoveredTool, ...]:
        """Return the server's tool catalog as a tuple of `DiscoveredTool`."""
        session = self._require_session()
        try:
            result = await asyncio.wait_for(
                session.list_tools(), timeout=self._timeout.total_seconds()
            )
        except TimeoutError as exc:
            raise McpToolError("list_tools timeout") from exc
        return tuple(
            DiscoveredTool(
                name=t.name,
                description=t.description or "",
                # `inputSchema` is the JSON Schema dict; SDK already parsed it.
                input_schema=dict(t.inputSchema) if t.inputSchema else {},
            )
            for t in result.tools
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke a tool. Returns the SDK's structured result (`structuredContent`
        if present, else the raw content list)."""
        session = self._require_session()
        try:
            result = await asyncio.wait_for(
                session.call_tool(name, arguments=arguments),
                timeout=self._timeout.total_seconds(),
            )
        except TimeoutError as exc:
            raise McpToolError(f"call_tool({name!r}) timeout") from exc
        if result.isError:
            raise McpToolError(
                f"mcp tool {name!r} returned isError=True: {result.content!r}"
            )
        # Prefer structured content (MCP 2025+) over the loose content list.
        if result.structuredContent is not None:
            return result.structuredContent
        return result.content


__all__ = [
    "DiscoveredTool",
    "McpStartupError",
    "McpToolError",
    "StdioMcpClient",
]
