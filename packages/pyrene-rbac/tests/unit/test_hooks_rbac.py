"""RBAC hook factory + gateway wiring unit tests.

Verifies:
  - The hook factory produces a `BeforeRunHook` that the gateway
    accepts at `priority=PRIORITY_TOOL_RBAC` (= 20).
  - `register_hooks(...)` registers exactly one before-hook at the
    canonical priority.
  - `tool_name is None` → no DB lookup, no veto (agent-run entry).
  - Deny path raises `PermissionDeniedError` with a user-language
    message including the tool name + role name + next action.
  - Allow path returns silently and lets the agent run downstream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core import UserContext
from pyrene_core.errors import PermissionDeniedError
from pyrene_gateway import (
    PRIORITY_TOOL_RBAC,
    Gateway,
    RunContext,
)
from pyrene_rbac import permission_resolver as pr_module
from pyrene_rbac.hooks import make_rbac_hook
from pyrene_rbac.permission_resolver import PermissionResolver
from pyrene_rbac.startup import register_hooks

# -------------------- Helpers --------------------


class _FakeSession:
    """No-op AsyncSession stand-in (resolver path is monkeypatched)."""


async def _session_factory() -> AsyncIterator[AsyncSession]:
    yield cast(AsyncSession, _FakeSession())


def _ctx(tool_name: str | None, role_names: tuple[str, ...] = ("viewer",)) -> RunContext:
    user = UserContext(user_id=uuid4(), team_id=uuid4(), roles=role_names)
    return RunContext(
        user_context=user, request_id=uuid4(), tool_name=tool_name
    )


class _FakePermission:
    def __init__(self, role_id: UUID, tool_name: str, action: str) -> None:
        self.role_id = role_id
        self.tool_name = tool_name
        self.action = action


def _patch_resolver_lookup(
    monkeypatch: pytest.MonkeyPatch, rows: list[_FakePermission]
) -> None:
    async def _fake_lookup(
        session: Any, role_ids: Any, tool_name: str
    ) -> list[_FakePermission]:
        role_id_tuple = tuple(role_ids)
        return [
            r
            for r in rows
            if r.tool_name == tool_name and r.role_id in role_id_tuple
        ]

    monkeypatch.setattr(
        pr_module, "list_permissions_for_roles", _fake_lookup
    )


def _make_role_lookup(
    mapping: dict[str, UUID],
) -> Any:
    async def _lookup(_session: Any, names: tuple[str, ...]) -> tuple[UUID, ...]:
        return tuple(mapping[n] for n in names if n in mapping)

    return _lookup


# -------------------- Tool name None: skip --------------------


async def test_hook_skips_when_tool_name_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent-run entry has no tool_name → hook returns without DB hit."""
    role_id = uuid4()
    # If the resolver were consulted, lookup would raise (no row).
    _patch_resolver_lookup(monkeypatch, [])

    resolver = PermissionResolver()
    hook = make_rbac_hook(
        resolver,
        session_factory=_session_factory,
        role_lookup=_make_role_lookup({"viewer": role_id}),
    )

    await hook(_ctx(tool_name=None))
    # No cache entry created — confirms zero DB work.
    assert resolver._cache_size() == 0


# -------------------- Allow path --------------------


async def test_hook_allow_path_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = uuid4()
    _patch_resolver_lookup(
        monkeypatch, [_FakePermission(viewer, "run_select", "allow")]
    )
    resolver = PermissionResolver()
    hook = make_rbac_hook(
        resolver,
        session_factory=_session_factory,
        role_lookup=_make_role_lookup({"viewer": viewer}),
    )
    # No raise → allow.
    await hook(_ctx(tool_name="run_select"))


# -------------------- Deny path --------------------


async def test_hook_default_deny_raises_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence of allow row → default deny → PermissionDeniedError."""
    _patch_resolver_lookup(monkeypatch, [])
    resolver = PermissionResolver()
    hook = make_rbac_hook(
        resolver,
        session_factory=_session_factory,
        role_lookup=_make_role_lookup({"viewer": uuid4()}),
    )
    with pytest.raises(PermissionDeniedError) as exc_info:
        await hook(_ctx(tool_name="run_aggregate"))
    msg = str(exc_info.value)
    assert "run_aggregate" in msg
    assert "viewer" in msg
    assert "관리자에게" in msg


async def test_hook_explicit_deny_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = uuid4()
    _patch_resolver_lookup(
        monkeypatch,
        [
            _FakePermission(viewer, "run_select", "allow"),
            _FakePermission(viewer, "run_select", "deny"),
        ],
    )
    resolver = PermissionResolver()
    hook = make_rbac_hook(
        resolver,
        session_factory=_session_factory,
        role_lookup=_make_role_lookup({"viewer": viewer}),
    )
    with pytest.raises(PermissionDeniedError):
        await hook(_ctx(tool_name="run_select"))


async def test_hook_deny_message_lists_multiple_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-role user → message lists all roles (no fake-single-role lie)."""
    _patch_resolver_lookup(monkeypatch, [])
    resolver = PermissionResolver()
    hook = make_rbac_hook(
        resolver,
        session_factory=_session_factory,
        role_lookup=_make_role_lookup({"viewer": uuid4(), "analyst": uuid4()}),
    )
    with pytest.raises(PermissionDeniedError) as exc_info:
        await hook(_ctx(tool_name="run_join", role_names=("viewer", "analyst")))
    msg = str(exc_info.value)
    assert "viewer" in msg
    assert "analyst" in msg


# -------------------- Gateway registration --------------------


async def test_register_hooks_uses_priority_tool_rbac() -> None:
    """`register_hooks` MUST register at PRIORITY_TOOL_RBAC = 20."""
    gateway = Gateway()
    resolver = PermissionResolver()
    register_hooks(
        gateway,
        resolver=resolver,
        session_factory=_session_factory,
        role_lookup=_make_role_lookup({}),
    )
    # Inspection: gateway exposes the registered hooks; we cannot read
    # priority directly off `before_hooks()` (it returns the Callable
    # only), so we verify by registering a sentinel BEFORE/AFTER the
    # canonical priority and observing ordering.
    log: list[str] = []

    async def lower(ctx: RunContext) -> None:
        log.append("pre")

    async def higher(ctx: RunContext) -> None:
        log.append("post")

    gateway.before_run(lower, priority=PRIORITY_TOOL_RBAC - 1)
    gateway.before_run(higher, priority=PRIORITY_TOOL_RBAC + 1)

    # Execute the chain manually — we cannot run a real agent in unit
    # tests, but the gateway exposes the before-hook list for tests.
    user = UserContext(
        user_id=uuid4(), team_id=uuid4(), roles=()
    )  # no roles → deny on rbac hook
    ctx = RunContext(
        user_context=user, request_id=uuid4(), tool_name="run_select"
    )

    hooks = gateway.before_hooks()
    assert len(hooks) == 3
    # First hook runs (PRIORITY_TOOL_RBAC - 1).
    await hooks[0](ctx)
    # Second hook (RBAC at PRIORITY_TOOL_RBAC) raises PermissionDenied
    # because role_lookup maps no names → empty role_ids → deny.
    with pytest.raises(PermissionDeniedError):
        await hooks[1](ctx)
    # Third hook (PRIORITY_TOOL_RBAC + 1) runs only if reached — the
    # gateway would normally stop after the raise. Here we just
    # confirm it exists in the chain at the correct slot.
    await hooks[2](ctx)

    assert log == ["pre", "post"]


async def test_register_hooks_registers_exactly_one_hook() -> None:
    gateway = Gateway()
    resolver = PermissionResolver()
    before_count = len(gateway.before_hooks())
    register_hooks(
        gateway,
        resolver=resolver,
        session_factory=_session_factory,
        role_lookup=_make_role_lookup({}),
    )
    after_count = len(gateway.before_hooks())
    assert after_count - before_count == 1
    # And nothing on the after_run side.
    assert gateway.after_hooks() == ()


async def test_register_hooks_priority_position_in_chain() -> None:
    """RBAC hook sits between PRIORITY_BUDGET_PRE (10) and PRIORITY_DATA_RBAC (30)."""
    from pyrene_gateway import PRIORITY_BUDGET_PRE, PRIORITY_DATA_RBAC

    gateway = Gateway()
    log: list[int] = []

    async def budget_pre(ctx: RunContext) -> None:
        log.append(PRIORITY_BUDGET_PRE)

    async def data_rbac(ctx: RunContext) -> None:
        log.append(PRIORITY_DATA_RBAC)

    gateway.before_run(data_rbac, priority=PRIORITY_DATA_RBAC)
    gateway.before_run(budget_pre, priority=PRIORITY_BUDGET_PRE)

    resolver = PermissionResolver()
    register_hooks(
        gateway,
        resolver=resolver,
        session_factory=_session_factory,
        role_lookup=_make_role_lookup({}),
    )

    # RBAC hook is the 2nd in execution order.
    hooks = gateway.before_hooks()
    assert len(hooks) == 3

    # Run the chain — budget_pre runs first; rbac runs second (and is
    # the only one that raises because role_lookup returns empty);
    # data_rbac never runs in the canonical chain because the gateway
    # propagates the raise.
    user = UserContext(user_id=uuid4(), team_id=uuid4(), roles=())
    ctx = RunContext(
        user_context=user, request_id=uuid4(), tool_name="run_select"
    )

    await hooks[0](ctx)
    assert log == [PRIORITY_BUDGET_PRE]

    with pytest.raises(PermissionDeniedError):
        await hooks[1](ctx)

    # The third (data_rbac at 30) is downstream of RBAC — confirms ordering.
    await hooks[2](ctx)
    assert log == [PRIORITY_BUDGET_PRE, PRIORITY_DATA_RBAC]
