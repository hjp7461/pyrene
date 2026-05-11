"""Gateway hook end-to-end against real Postgres (PROJECT_BRIEF §3.2 scenario A).

Combines the resolver + the RBAC hook + the gateway hook chain. The
agent itself is stubbed (we are testing the policy gate, not the
agent runtime — that lives in pyrene-agents).

Concrete scenario (PRD-010 §6 ✅ checkbox):
  - analyst → run_select allowed → hook returns silently → agent runs
  - viewer  → run_aggregate denied → hook raises PermissionDeniedError
  - admin   → run_aggregate allowed (admin holds explicit allow row)

Implementation note: `Gateway.run(...)` builds the `RunContext` with
`tool_name=None` for the agent-run entry point (PLAN-009 design — a
single agent run may invoke multiple tools internally). Our hook
short-circuits when `tool_name is None`. To exercise the tool-RBAC
path we construct a `RunContext` with `tool_name` pinned and run the
before_hooks() chain directly. PLAN-013/014 budget hooks follow the
same testing pattern.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.models import Role
from pyrene_core import UserContext
from pyrene_core.errors import PermissionDeniedError
from pyrene_gateway import (
    PRIORITY_TOOL_RBAC,
    Gateway,
    RunContext,
)
from pyrene_rbac import PermissionResolver, make_rbac_hook
from pyrene_rbac.models import Permission

pytestmark = pytest.mark.integration


async def _seed_matrix(
    db_session: AsyncSession,
) -> tuple[Role, Role, Role]:
    """Seed three roles + the §3.2 matrix.

    Returns `(viewer, analyst, admin)` Role rows.
    """
    suffix = uuid4().hex[:8]
    viewer = Role(name=f"viewer-{suffix}", description="")
    analyst = Role(name=f"analyst-{suffix}", description="")
    admin = Role(name=f"admin-{suffix}", description="")
    db_session.add_all([viewer, analyst, admin])
    await db_session.flush()

    db_session.add_all(
        [
            Permission(role_id=viewer.id, tool_name="run_select", action="allow"),
            Permission(role_id=analyst.id, tool_name="run_select", action="allow"),
            Permission(role_id=analyst.id, tool_name="run_aggregate", action="allow"),
            Permission(role_id=admin.id, tool_name="run_select", action="allow"),
            Permission(role_id=admin.id, tool_name="run_aggregate", action="allow"),
        ]
    )
    await db_session.flush()
    return viewer, analyst, admin


async def _drive_hook_chain(
    db_session: AsyncSession,
    *,
    resolver: PermissionResolver,
    role: Role,
    tool_name: str,
) -> None:
    """Drive the canonical before_hooks() chain with a pinned tool_name.

    Returns None on allow, raises `PermissionDeniedError` on deny.
    Mirrors how `Gateway.run()` walks `before_hooks()` — except we
    build the context ourselves so `tool_name` is observable.
    """

    async def _session_factory() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _role_lookup(
        _session: AsyncSession, _names: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        return (role.id,)

    gateway = Gateway()
    hook = make_rbac_hook(
        resolver,
        session_factory=_session_factory,
        role_lookup=_role_lookup,
    )
    gateway.before_run(hook, priority=PRIORITY_TOOL_RBAC)

    user_ctx = UserContext(
        user_id=uuid4(), team_id=uuid4(), roles=(role.name,)
    )
    ctx = RunContext(
        user_context=user_ctx,
        request_id=uuid4(),
        tool_name=tool_name,
        question="q",
    )
    for before in gateway.before_hooks():
        await before(ctx)


async def test_analyst_run_select_allowed(db_session: AsyncSession) -> None:
    _, analyst, _ = await _seed_matrix(db_session)
    resolver = PermissionResolver()
    # No raise → allow.
    await _drive_hook_chain(
        db_session,
        resolver=resolver,
        role=analyst,
        tool_name="run_select",
    )


async def test_analyst_run_aggregate_allowed(db_session: AsyncSession) -> None:
    _, analyst, _ = await _seed_matrix(db_session)
    resolver = PermissionResolver()
    await _drive_hook_chain(
        db_session,
        resolver=resolver,
        role=analyst,
        tool_name="run_aggregate",
    )


async def test_viewer_run_aggregate_denied(db_session: AsyncSession) -> None:
    """PROJECT_BRIEF §3.2 scenario A — viewer is allow-listed for run_select
    only; run_aggregate must be denied with the user-language message."""
    viewer, _, _ = await _seed_matrix(db_session)
    resolver = PermissionResolver()
    with pytest.raises(PermissionDeniedError) as exc_info:
        await _drive_hook_chain(
            db_session,
            resolver=resolver,
            role=viewer,
            tool_name="run_aggregate",
        )
    msg = str(exc_info.value)
    assert "run_aggregate" in msg
    assert viewer.name in msg
    assert "관리자" in msg


async def test_admin_run_aggregate_allowed(db_session: AsyncSession) -> None:
    _, _, admin = await _seed_matrix(db_session)
    resolver = PermissionResolver()
    await _drive_hook_chain(
        db_session,
        resolver=resolver,
        role=admin,
        tool_name="run_aggregate",
    )


async def test_unknown_tool_denied_by_default(db_session: AsyncSession) -> None:
    """Default-deny (PRD-010 §2.2 F1) — no row → deny."""
    _, analyst, _ = await _seed_matrix(db_session)
    resolver = PermissionResolver()
    with pytest.raises(PermissionDeniedError):
        await _drive_hook_chain(
            db_session,
            resolver=resolver,
            role=analyst,
            tool_name="run_dml",
        )
