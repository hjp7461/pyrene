"""ToolRegistry: name → Pydantic AI tool callable mapping.

Phase 2 agents reference tools by string name in their AgentVersion row.
The registry resolves those names to actual `(ctx, input) -> output`
callables that the builder hands to `@agent.tool(...)`.

Phase 1 SQL analyst exposes three tool functions on the `sql_analyst`
agent (`run_select`, `run_join`, `run_aggregate`). PLAN-009 (MCP gateway)
will swap this registry for an out-of-process dispatcher; until then we
proxy to the existing Phase 1 callables.

Why a class instead of a module-level dict:
  - Multiple registries can coexist (Phase 2 + Phase 1 fallback, per-team
    overrides, test isolation).
  - `register(...)` is explicit and traceable in logs.
  - `resolve(...)` raises a `ToolNotRegisteredError` (subclass of KeyError)
    that the builder converts into a structured 422.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# Tool callables follow Pydantic AI's `(ctx, input) -> output` contract.
# `Any` here is unavoidable at the registry boundary: each registered tool
# has its own input / output Pydantic models. Builder-side construction
# carries the concrete types through `@agent.tool(...)` decoration, so
# mypy --strict only sees `Any` at the registry edge — not in user code.
ToolCallable = Callable[..., Awaitable[Any]]


class ToolNotRegisteredError(KeyError):
    """Raised when a builder asks for a tool name that isn't in the registry."""


class ToolRegistry:
    """Mutable mapping of tool name → callable.

    Instances are typically constructed once at app startup with the
    canonical tools (`run_select`, `run_join`, `run_aggregate`) and then
    passed read-only into builder calls.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolCallable] = {}

    def register(self, name: str, fn: ToolCallable) -> None:
        """Register `fn` under `name`. Re-registration overwrites silently —
        host apps may rebind a tool during tests or hot upgrades.
        """
        self._tools[name] = fn

    def resolve(self, name: str) -> ToolCallable:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotRegisteredError(
                f"tool {name!r} is not registered; "
                f"available: {sorted(self._tools)}"
            ) from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools


def default_tool_registry() -> ToolRegistry:
    """Build the canonical Phase 2 registry with the three SQL analyst tools.

    Re-imports `pyrene_sql.agent` lazily to keep `import pyrene_agents` cheap
    when only the schema layer is needed (e.g. yaml load / export with no
    DB). The Phase 1 `sql_analyst` decorators already attach these to the
    underlying Pydantic AI Agent, but for builder-driven re-registration we
    expose the underlying callables on the registry directly.
    """
    from pyrene_sql.agent import run_aggregate, run_join, run_select

    registry = ToolRegistry()
    # Pydantic AI's `@agent.tool` decorator (1.93) returns the original async
    # function unchanged (the agent stores the tool internally). So we can
    # register the bare callables and pass them to a fresh agent's `.tool()`
    # later without losing context.
    registry.register("run_select", run_select)
    registry.register("run_join", run_join)
    registry.register("run_aggregate", run_aggregate)
    return registry


__all__ = [
    "ToolCallable",
    "ToolNotRegisteredError",
    "ToolRegistry",
    "default_tool_registry",
]
