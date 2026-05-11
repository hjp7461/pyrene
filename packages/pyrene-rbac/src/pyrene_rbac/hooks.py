"""`before_run` hook factory for tool-level RBAC.

PLAN-010 Day 2. The hook closes over:

  - a `PermissionResolver`        — cached decision oracle
  - a `session_factory: () -> AsyncSession`
                                  — opens a fresh session per check
                                    so the hook is not tied to the
                                    FastAPI request session lifecycle

It reads `RunContext.tool_name` + `RunContext.user_context.roles` to
formulate the decision. Two policy choices baked in:

  1. **`tool_name is None` -> skip.** The gateway also serves the
     "agent run" entry where the agent decides its own tool sequence
     (PLAN-009 RunContext §). Tool-level RBAC fires per-tool-call;
     the agent-run entry hands tool decisions to PLAN-011 + the
     agent's own `Deps` instead. PRD-010 §3.1 is unambiguous: the
     matrix protects **tool invocations**.

  2. **Role names -> role_ids resolution.** `UserContext.roles` is a
     `tuple[str, ...]` of names (PRD-007 §4). The hook resolves these
     to UUIDs via the `role_lookup` callback that the host app injects
     at construction time. The lookup is cached at the host-app
     layer (PLAN-007's `list_user_roles_for_team` already runs once
     per request).

### Fail-closed (PRD-010 §2.2 F2)

The hook raises `PermissionDeniedError` on every deny (default-deny
included). The gateway re-raises out of `Gateway.run(...)`; the
FastAPI handler maps it to HTTP 403 with the user-language message
from `errors.py`.

### Registration

```python
resolver = PermissionResolver()
hook = make_rbac_hook(resolver, session_factory=..., role_lookup=...)
gateway.before_run(hook, priority=PRIORITY_TOOL_RBAC)
```

The startup module (`pyrene_rbac.startup`) wraps this in
`register_hooks(gateway, ...)` for callers who do not want to think
about priority + factory wiring.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core.errors import PermissionDeniedError
from pyrene_gateway import BeforeRunHook, RunContext
from pyrene_rbac.permission_resolver import (
    DEFAULT_CONNECTION_ID,
    PermissionResolver,
)

# Callback the host app supplies at startup. Two forms accepted:
#
#  (a) plain coroutine:           `(names, team_id) -> Awaitable[tuple[UUID,...]]`
#  (b) async generator session:   handled by the session_factory below
#
# `role_lookup` is the bridge from `UserContext.roles` (names) to the
# UUIDs the resolver needs. PLAN-007 already exposes
# `list_user_roles_for_team` but it returns names; the host app wires
# a lookup-by-name function (or pre-computes a dict at login time).
RoleLookup = Callable[[AsyncSession, tuple[str, ...]], Awaitable[tuple[UUID, ...]]]

# Session factory: returns an async-iterator yielding one AsyncSession.
# Same shape as `pyrene_auth.dependencies._session_proxy` but invoked
# directly by the hook (no FastAPI request scope).
SessionFactory = Callable[[], AsyncIterator[AsyncSession]]


def _format_deny_message(tool_name: str, role_names: tuple[str, ...]) -> str:
    """User-language denial message — PROJECT_BRIEF §6.1-7 + PRD-010 §4.

    Includes the offending tool + the caller's roles + the next action.
    Multiple roles are listed because a user may carry several roles in
    one team (e.g. analyst + viewer); the message must not pretend the
    user has exactly one.
    """
    if not role_names:
        roles_phrase = "역할이 없는 사용자"
    elif len(role_names) == 1:
        roles_phrase = f"역할 '{role_names[0]}'"
    else:
        joined = ", ".join(f"'{r}'" for r in sorted(role_names))
        roles_phrase = f"역할 ({joined})"
    return (
        f"{roles_phrase}은(는) '{tool_name}' 도구를 호출할 권한이 없습니다. "
        "관리자에게 요청하세요."
    )


def make_rbac_hook(
    resolver: PermissionResolver,
    *,
    session_factory: SessionFactory,
    role_lookup: RoleLookup,
    connection_id: UUID = DEFAULT_CONNECTION_ID,
) -> BeforeRunHook:
    """Construct a `before_run` hook bound to `resolver` + DI seams.

    Returned callable has the `BeforeRunHook` Protocol shape:
    `async def(ctx: RunContext) -> None`.

    `connection_id` lets the host app pin a non-default connection id
    when PLAN-011 lands (Phase 2 uses the sentinel UUID).
    """

    # We accept either an async generator OR a plain async context
    # manager from `session_factory`. Wrap into an asynccontextmanager
    # so the hook body stays linear.
    @asynccontextmanager
    async def _session_cm() -> AsyncIterator[AsyncSession]:
        iterator = session_factory()
        # async generator path — iterate once, yield the session.
        async for session in iterator:
            yield session
            return

    async def _hook(ctx: RunContext) -> None:
        # Policy choice (1): no tool_name -> skip tool RBAC. The
        # agent-level path falls through to data RBAC (PLAN-011)
        # which receives the resolved tool inside the agent.
        if ctx.tool_name is None:
            return

        user = ctx.user_context
        async with _session_cm() as session:
            role_ids = await role_lookup(session, user.roles)
            allowed = await resolver.can_invoke(
                session,
                role_ids=role_ids,
                tool_name=ctx.tool_name,
                connection_id=connection_id,
            )

        if not allowed:
            raise PermissionDeniedError(
                _format_deny_message(ctx.tool_name, user.roles)
            )

    return _hook


__all__ = [
    "RoleLookup",
    "SessionFactory",
    "make_rbac_hook",
]
