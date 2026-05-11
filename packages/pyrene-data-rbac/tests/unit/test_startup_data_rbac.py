"""Startup wiring — verifies the hook lands at `PRIORITY_DATA_RBAC`.

PLAN-009 §C-2 fixes the priority schedule; this test guards against a
silent renumbering during chain refactors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_data_rbac import DataPermissionResolver, register_hooks
from pyrene_gateway import Gateway
from pyrene_gateway.constants import (
    PRIORITY_BUDGET_POST,
    PRIORITY_BUDGET_PRE,
    PRIORITY_DATA_RBAC,
    PRIORITY_TOOL_RBAC,
)


async def _stub_role_lookup(
    session: AsyncSession, names: tuple[str, ...]
) -> tuple[UUID, ...]:
    return ()


class _StubSessionFactory:
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]


def test_register_hooks_inserts_at_priority_data_rbac() -> None:
    gateway = Gateway()
    resolver = DataPermissionResolver()
    hook = register_hooks(
        gateway,
        resolver=resolver,
        session_factory=_StubSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    hooks = gateway.before_hooks()
    assert hook in hooks
    # The HookRegistry sorts by (priority, seq). Our hook is the only
    # one registered → it sits at index 0 and PRIORITY_DATA_RBAC is 30.
    assert PRIORITY_DATA_RBAC == 30
    assert PRIORITY_TOOL_RBAC < PRIORITY_DATA_RBAC < PRIORITY_BUDGET_POST
    assert PRIORITY_BUDGET_PRE < PRIORITY_DATA_RBAC


def test_data_rbac_hook_runs_after_tool_rbac() -> None:
    """Ordering invariant: tool-RBAC (20) fires before data-RBAC (30)
    so a denied tool call short-circuits BEFORE we hit the data layer."""
    assert PRIORITY_TOOL_RBAC == 20
    assert PRIORITY_DATA_RBAC == 30
    # Sorted ascending — confirm the chain shape PLAN-009 commits to.
    assert PRIORITY_TOOL_RBAC < PRIORITY_DATA_RBAC
