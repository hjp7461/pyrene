"""Spec → Pydantic AI Agent builder.

Day 2 of PLAN-008. Given an `AgentSpec` row + its current `AgentVersion`,
construct a Pydantic AI `Agent` instance bound to:

  - `output_type = OUTPUT_SCHEMA_REGISTRY[version.output_schema_key]`
    (Literal-validated at schema load; KeyError surfaces as a builder error
    if a DB row was written before a key was retired).
  - `system_prompt = version.system_prompt` (static base).
  - tools resolved from `ToolRegistry` by name; missing tool → builder error.

ADR-002 notes:
  - `defer_model_check=True` so the builder doesn't fail unit tests that
    have no provider key configured. The actual `agent.run(...)` call
    validates the model.
  - `@agent.tool(retries=0)` disables native retries — the external
    `RetryWrapper` (PLAN-003) owns retry semantics in `run_with_retry`.

Note on `Agent[Deps, Any]`:
  The return-type uses `Any` for the output type parameter because the
  builder is generic over Pydantic schemas chosen at runtime. The actual
  output_type is fully typed inside the agent via Pydantic AI's schema
  enforcement (terminal `final_result` tool), so this `Any` doesn't leak
  into the public response model (`AnalystResponse`, etc.). Run-path code
  treats the returned object as the registered `BaseModel` subclass.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic_ai import Agent

from pyrene_agents.models import AgentSpec, AgentVersion
from pyrene_agents.output_schemas import (
    OUTPUT_SCHEMA_REGISTRY,
    resolve_output_schema,
)
from pyrene_agents.tool_registry import (
    ToolNotRegisteredError,
    ToolRegistry,
    default_tool_registry,
)
from pyrene_sql.deps import Deps

_DEFAULT_MODEL = os.getenv("MODEL_NAME", "anthropic:claude-sonnet-4-6")


class AgentBuildError(ValueError):
    """Raised when a spec/version cannot be turned into an Agent.

    Subclasses `ValueError` so FastAPI handlers can map it to 422 without
    a custom handler (or callers can re-raise as `HTTPException`).
    """


def build_agent(
    spec: AgentSpec,
    version: AgentVersion,
    *,
    tool_registry: ToolRegistry | None = None,
    model_name: str | None = None,
) -> Agent[Deps, Any]:
    """Construct a Pydantic AI Agent from a spec/version pair.

    `tool_registry` defaults to `default_tool_registry()` (the three Phase 1
    SQL tools). Tests pass a fresh registry with mocked callables.

    Raises:
      AgentBuildError: output_schema_key unknown, or a tool name is not in
                       the registry.
    """
    if version.output_schema_key not in OUTPUT_SCHEMA_REGISTRY:
        raise AgentBuildError(
            f"AgentVersion {version.id} references unknown output_schema_key "
            f"{version.output_schema_key!r}; "
            f"registered: {sorted(OUTPUT_SCHEMA_REGISTRY)}"
        )

    registry = tool_registry if tool_registry is not None else default_tool_registry()
    # Validate tool names BEFORE constructing the Agent so we fail fast with
    # a complete list of missing names (better DX than first-missing-only).
    missing: list[str] = [name for name in version.tools if name not in registry]
    if missing:
        raise AgentBuildError(
            f"AgentVersion {version.id} references unregistered tools: "
            f"{missing}; available: {list(registry.names())}"
        )

    output_type = resolve_output_schema(version.output_schema_key)
    chosen_model = model_name if model_name is not None else _DEFAULT_MODEL

    # Pydantic AI's `Agent.__init__` is generic over output_type; the
    # registry returns `type[BaseModel]`, so the runtime contract holds.
    # mypy --strict can't narrow `type[BaseModel]` to the specific subclass
    # at the call site, but the spec name (e.g. "AnalystResponse") flows
    # through the Literal layer at the schema boundary.
    agent: Agent[Deps, Any] = Agent(
        model=chosen_model,
        output_type=output_type,
        deps_type=Deps,
        system_prompt=version.system_prompt,
        defer_model_check=True,
    )

    for tool_name in version.tools:
        # We already validated above, but keep the resolve() call so a
        # racey registry mutation surfaces as ToolNotRegisteredError, which
        # we convert into AgentBuildError for the caller.
        try:
            fn = registry.resolve(tool_name)
        except ToolNotRegisteredError as exc:  # pragma: no cover - guarded above
            raise AgentBuildError(str(exc)) from exc
        # `retries=0` per ADR-002: external wrapper owns retry; built-in retry off.
        # `name=tool_name` ensures the model sees the spec-declared name even
        # if the callable was registered under a different `__name__`.
        agent.tool(retries=0, name=tool_name)(fn)

    # Suppress unused-import warning: spec is part of the public signature
    # so callers can pass both objects without re-fetching from DB. We
    # don't read fields off `spec` in the builder body itself (yet) — the
    # name + team_id are used by the run endpoint and logging layer.
    _ = spec
    return agent


__all__ = ["AgentBuildError", "build_agent"]
