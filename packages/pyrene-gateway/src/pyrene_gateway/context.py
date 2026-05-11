"""RunContext — the immutable payload threaded through the hook chain.

Every `Gateway.run(...)` invocation constructs one `RunContext`
instance and passes it to each `before_run` / `after_run` hook in
priority order. PLAN-010 reads `user_context.roles` for tool RBAC;
PLAN-011 reads `tool_name` + `team_id` for data RBAC; PLAN-014 reads
`agent_id` + `team_id` for budget rollups; PLAN-015 stamps the
`request_id` onto its `AuditEvent`.

Design choices:
  - `StrictBaseModel` + frozen. Hooks must not mutate fields — if a
    hook needs to attach state, it does so via a Pydantic AI Deps
    extension (Phase 3) or its own per-request store.
  - `tool_name` is optional because the gateway also serves the
    "agent run" entry point where no specific tool is named upfront
    (the agent decides). PLAN-010 hook tolerates `tool_name=None` by
    skipping tool-RBAC (it falls through to data-RBAC at PRIORITY 30).
  - `metadata` is a sink-opaque dict for cross-hook handoff
    (e.g. PLAN-014 pre-flight stamps `budget_remaining`; PLAN-015
    audit reads it). Frozen at the Pydantic level means callers must
    rebuild a fresh `RunContext` to amend metadata — for Phase 2 we
    accept the cost (5-10 hooks per run).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import Field

from pyrene_core import StrictBaseModel, UserContext


class RunContext(StrictBaseModel):
    """Context for a single gateway-routed agent run or tool call.

    Carries enough identity + correlation handles for every hook to
    enforce policy + emit audit without DB lookups inside the hook
    itself. Hooks that need richer state (DB session, MCP client) take
    that via closure at registration time, not via `RunContext`.
    """

    user_context: UserContext
    agent_id: UUID | None = None
    request_id: UUID
    tool_name: str | None = None
    question: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = ["RunContext"]
