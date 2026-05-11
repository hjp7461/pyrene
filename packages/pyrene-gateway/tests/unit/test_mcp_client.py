"""Unit tests for `StdioMcpClient` — round-trip + lifecycle + error paths.

PLAN-009 Day 2. We stub the official SDK's `stdio_client` + `ClientSession`
so the test does not spawn a real subprocess. The wrapper's contract is
that it (a) drives `initialize()`, (b) projects `ListToolsResult.tools`
into `DiscoveredTool` tuples, (c) projects `CallToolResult` either as
`structuredContent` or `content`, (d) propagates timeouts.

We monkey-patch the symbols inside `pyrene_gateway.mcp_client` since the
wrapper imports them at module load.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from pyrene_gateway import mcp_client as mc
from pyrene_gateway.mcp_client import (
    DiscoveredTool,
    McpStartupError,
    McpToolError,
    StdioMcpClient,
)


class _FakeTool:
    def __init__(self, name: str, description: str, input_schema: dict[str, Any]) -> None:
        self.name = name
        self.description = description
        self.inputSchema = input_schema


class _FakeListToolsResult:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self.tools = tools


class _FakeCallToolResult:
    def __init__(
        self,
        *,
        structured: Any = None,
        content: Any = None,
        is_error: bool = False,
    ) -> None:
        self.structuredContent = structured
        self.content = content
        self.isError = is_error


class _FakeSession:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools = tools
        self.initialized = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self) -> _FakeListToolsResult:
        return _FakeListToolsResult(self._tools)

    async def call_tool(
        self, name: str, *, arguments: dict[str, Any] | None = None
    ) -> _FakeCallToolResult:
        self.calls.append((name, arguments or {}))
        if name == "fail":
            return _FakeCallToolResult(is_error=True, content="boom")
        return _FakeCallToolResult(structured={"echo": arguments})

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@asynccontextmanager
async def _fake_stdio_client(params: object):  # type: ignore[no-untyped-def]
    yield (object(), object())  # read/write streams — not used by fake session


def _install_fakes(monkeypatch: pytest.MonkeyPatch, tools: list[_FakeTool]) -> _FakeSession:
    session = _FakeSession(tools)

    def _fake_session_factory(*args: object, **kwargs: object) -> _FakeSession:
        return session

    monkeypatch.setattr(mc, "stdio_client", _fake_stdio_client)
    monkeypatch.setattr(mc, "ClientSession", _fake_session_factory)
    return session


async def test_start_initializes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, tools=[])
    client = StdioMcpClient(command="echo")
    await client.start()
    # Idempotent
    await client.start()
    await client.stop()


async def test_list_tools_projects_to_discovered_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(
        monkeypatch,
        tools=[
            _FakeTool("echo", "echoes input", {"type": "object"}),
            _FakeTool("ping", "", {}),
        ],
    )
    async with StdioMcpClient(command="echo") as client:
        tools = await client.list_tools()
    assert tools == (
        DiscoveredTool(name="echo", description="echoes input", input_schema={"type": "object"}),
        DiscoveredTool(name="ping", description="", input_schema={}),
    )


async def test_call_tool_structured_content_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, tools=[])
    async with StdioMcpClient(command="echo") as client:
        result = await client.call_tool("echo", {"text": "hi"})
    assert result == {"echo": {"text": "hi"}}


async def test_call_tool_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, tools=[])
    async with StdioMcpClient(command="echo") as client:
        with pytest.raises(McpToolError, match="isError=True"):
            await client.call_tool("fail", {})


async def test_require_session_when_not_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_tool / list_tools before start → McpStartupError."""
    _install_fakes(monkeypatch, tools=[])
    client = StdioMcpClient(command="echo")
    with pytest.raises(McpStartupError, match="not started"):
        await client.list_tools()


async def test_start_failure_surfaces_as_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the underlying stdio_client raises, start() converts to McpStartupError."""

    @asynccontextmanager
    async def _broken(params: object):  # type: ignore[no-untyped-def]
        raise OSError("no such file")
        yield  # unreachable; for typing

    monkeypatch.setattr(mc, "stdio_client", _broken)
    client = StdioMcpClient(command="/does/not/exist")
    with pytest.raises(McpStartupError):
        await client.start()


async def test_stop_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, tools=[])
    client = StdioMcpClient(command="echo")
    # stop without start is no-op.
    await client.stop()
    await client.start()
    await client.stop()
    await client.stop()  # second stop is no-op
