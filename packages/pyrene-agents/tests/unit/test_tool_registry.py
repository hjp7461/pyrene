"""Unit tests for `ToolRegistry`.

Covers register / resolve / __contains__ / names and the
`default_tool_registry()` factory.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrene_agents.tool_registry import (
    ToolNotRegisteredError,
    ToolRegistry,
    default_tool_registry,
)


async def _dummy_tool(_ctx: Any, _input: Any) -> dict[str, Any]:
    return {"ok": True}


def test_register_and_resolve() -> None:
    registry = ToolRegistry()
    registry.register("dummy", _dummy_tool)
    assert "dummy" in registry
    assert registry.resolve("dummy") is _dummy_tool


def test_resolve_unknown_raises() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolNotRegisteredError) as exc_info:
        registry.resolve("nope")
    assert "nope" in str(exc_info.value)


def test_names_returns_sorted_tuple() -> None:
    registry = ToolRegistry()
    registry.register("b", _dummy_tool)
    registry.register("a", _dummy_tool)
    registry.register("c", _dummy_tool)
    assert registry.names() == ("a", "b", "c")


def test_register_overwrites_existing() -> None:
    registry = ToolRegistry()
    registry.register("dummy", _dummy_tool)

    async def replacement(_ctx: Any, _input: Any) -> dict[str, Any]:
        return {"ok": False}

    registry.register("dummy", replacement)
    assert registry.resolve("dummy") is replacement


def test_default_registry_carries_phase1_tools() -> None:
    registry = default_tool_registry()
    assert set(registry.names()) >= {"run_select", "run_join", "run_aggregate"}


def test_contains_rejects_non_strings() -> None:
    registry = ToolRegistry()
    registry.register("x", _dummy_tool)
    assert 42 not in registry
    assert None not in registry
