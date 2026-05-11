"""Data-RBAC hook + schema-qualified canonicalization unit tests.

These tests pin the schema-qualified bypass surface (PM amend cases
1-5) and verify the hook's fail-closed contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core import UserContext
from pyrene_core.errors import PermissionDeniedError
from pyrene_data_rbac import (
    DataPermissionResolver,
    make_data_rbac_hook,
    parse_qualified,
)
from pyrene_data_rbac import permission_resolver as pr_module
from pyrene_data_rbac.permission_resolver import DEFAULT_CONNECTION_ID
from pyrene_gateway import RunContext

# ===========================================================================
# parse_qualified — schema-qualified bypass cases 1-5 (PM amend)
# ===========================================================================


def test_parse_qualified_plain() -> None:
    assert parse_qualified("public.payment") == ("public", "payment")


def test_parse_qualified_case_1_quoted_schema() -> None:
    """Case #1: `"public".payment` → `public.payment`."""
    assert parse_qualified('"public".payment') == ("public", "payment")


def test_parse_qualified_case_2_quoted_both() -> None:
    """Case #2: `"public"."payment"` → `public.payment`."""
    assert parse_qualified('"public"."payment"') == ("public", "payment")


def test_parse_qualified_case_3_uppercase() -> None:
    """Case #3: `PUBLIC.payment` → `public.payment`."""
    assert parse_qualified("PUBLIC.payment") == ("public", "payment")


def test_parse_qualified_case_4_whitespace() -> None:
    """Case #4: `public . payment` (spaces) → `public.payment`."""
    assert parse_qualified("public . payment") == ("public", "payment")


def test_parse_qualified_case_5_table_uppercase() -> None:
    """Case #5: `public.PAYMENT` → `public.payment`."""
    assert parse_qualified("public.PAYMENT") == ("public", "payment")


def test_parse_qualified_rejects_non_ascii_table() -> None:
    """Bypass-attempt extension: non-ASCII identifiers are rejected
    because the downstream SQL builder accepts only `[a-z_][a-z0-9_]*`
    (PRD-001 §4.1). Refusing here keeps the hook layer aligned."""
    assert parse_qualified("public.결제") is None


def test_parse_qualified_rejects_union_injection() -> None:
    """Multi-statement / UNION JOIN bypass attempts surface as
    unparseable identifiers — the regex on each side never matches a
    fragment containing keywords / spaces between tokens."""
    assert parse_qualified("public.payment; DROP TABLE x") is None
    assert parse_qualified("public.payment UNION SELECT 1") is None
    assert parse_qualified("public.(SELECT 1)") is None


def test_parse_qualified_rejects_unqualified() -> None:
    """A bare table name is rejected — PRD-001 requires `schema.table`."""
    assert parse_qualified("payment") is None


def test_parse_qualified_rejects_empty() -> None:
    assert parse_qualified("") is None
    assert parse_qualified(".") is None
    assert parse_qualified("public.") is None
    assert parse_qualified(".payment") is None


# ===========================================================================
# make_data_rbac_hook — fail-closed contract + tool-input extraction
# ===========================================================================


class _FakeRow:
    def __init__(
        self,
        role_id: UUID,
        connection_id: UUID,
        schema: str,
        table_name: str,
        action: str,
    ) -> None:
        self.role_id = role_id
        self.connection_id = connection_id
        self.schema = schema
        self.table_name = table_name
        self.action = action


def _install_lookup(
    monkeypatch: pytest.MonkeyPatch, rows: list[_FakeRow]
) -> None:
    async def _fake_lookup(
        session: Any,
        role_ids: Any,
        connection_id: UUID,
    ) -> list[_FakeRow]:
        role_id_tuple = tuple(role_ids)
        return [
            r
            for r in rows
            if r.role_id in role_id_tuple and r.connection_id == connection_id
        ]

    monkeypatch.setattr(
        pr_module, "list_permissions_for_roles_on_connection", _fake_lookup
    )


class _FakeSessionFactory:
    """Mimics the `async for session in factory()` contract."""

    async def __call__(self) -> AsyncIterator[AsyncSession]:
        # Real hooks pass the session into list_permissions_for_roles_on_connection,
        # which is monkeypatched in tests — so the value here is unused.
        yield None  # type: ignore[misc]


async def _stub_role_lookup(
    session: AsyncSession, names: tuple[str, ...]
) -> tuple[UUID, ...]:
    """Map role names to deterministic UUIDs.

    Tests stamp the desired role UUIDs into a module-level dict via
    `_set_role_map(...)` so the stub returns the canonical id.
    """
    return tuple(_ROLE_MAP[n] for n in names if n in _ROLE_MAP)


_ROLE_MAP: dict[str, UUID] = {}


def _set_role_map(**names_to_uuid: UUID) -> None:
    _ROLE_MAP.clear()
    _ROLE_MAP.update(names_to_uuid)


def _make_ctx(
    *,
    tool_name: str | None,
    tool_input: Any = None,
    connection_id: UUID | None = None,
    user_roles: tuple[str, ...] = ("analyst",),
) -> RunContext:
    metadata: dict[str, Any] = {}
    if tool_input is not None:
        metadata["tool_input"] = tool_input
    if connection_id is not None:
        metadata["connection_id"] = connection_id
    user = UserContext(
        user_id=uuid4(),
        team_id=uuid4(),
        roles=user_roles,
    )
    return RunContext(
        user_context=user,
        request_id=uuid4(),
        tool_name=tool_name,
        question="?",
        metadata=metadata,
    )


async def test_hook_allows_when_explicit_grant_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_id = uuid4()
    _set_role_map(analyst=role_id)
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(
                role_id, DEFAULT_CONNECTION_ID, "public", "payment", "allow"
            ),
        ],
    )
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_select",
        tool_input={"table": "public.payment"},
    )
    # No exception → allowed.
    await hook(ctx)


async def test_hook_denies_when_no_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_id = uuid4()
    _set_role_map(analyst=role_id)
    _install_lookup(monkeypatch, [])
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_select",
        tool_input={"table": "public.payment"},
    )
    with pytest.raises(PermissionDeniedError, match="payment"):
        await hook(ctx)


async def test_hook_denies_bypass_case_quoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PM amend bypass case #2: `"public"."payment"` — caller has NO
    grant on `public.payment`. The hook normalizes the input via
    `parse_qualified` and the lookup denies.
    """
    role_id = uuid4()
    _set_role_map(viewer=role_id)
    # Grant on `film` only — not `payment`.
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(
                role_id, DEFAULT_CONNECTION_ID, "public", "film", "allow"
            ),
        ],
    )
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_select",
        tool_input={"table": '"public"."payment"'},
        user_roles=("viewer",),
    )
    with pytest.raises(PermissionDeniedError):
        await hook(ctx)


async def test_hook_denies_bypass_case_uppercase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PM amend bypass case #3: `PUBLIC.PAYMENT` collapses to
    `public.payment` — the viewer still has no grant."""
    role_id = uuid4()
    _set_role_map(viewer=role_id)
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(
                role_id, DEFAULT_CONNECTION_ID, "public", "film", "allow"
            ),
        ],
    )
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_select",
        tool_input={"table": "PUBLIC.PAYMENT"},
        user_roles=("viewer",),
    )
    with pytest.raises(PermissionDeniedError):
        await hook(ctx)


async def test_hook_denies_bypass_case_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PM amend bypass case #4: `public . payment` (spaces)."""
    role_id = uuid4()
    _set_role_map(viewer=role_id)
    _install_lookup(monkeypatch, [])
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_select",
        tool_input={"table": "public . payment"},
        user_roles=("viewer",),
    )
    with pytest.raises(PermissionDeniedError):
        await hook(ctx)


async def test_hook_denies_bypass_case_non_ascii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PM amend bypass case #5 extension: non-ASCII identifier.
    `parse_qualified` returns None → hook denies fail-closed."""
    role_id = uuid4()
    _set_role_map(viewer=role_id)
    _install_lookup(monkeypatch, [])
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_select",
        tool_input={"table": "public.결제"},
        user_roles=("viewer",),
    )
    with pytest.raises(PermissionDeniedError, match=r"valid 'schema\.table'"):
        await hook(ctx)


async def test_hook_denies_union_injection_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNION-style injection attempts are unparseable → deny."""
    role_id = uuid4()
    _set_role_map(viewer=role_id)
    _install_lookup(monkeypatch, [])
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_select",
        tool_input={"table": "public.payment UNION SELECT 1"},
        user_roles=("viewer",),
    )
    with pytest.raises(PermissionDeniedError):
        await hook(ctx)


async def test_hook_skips_when_tool_name_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent-run entry (no specific tool) → hook is a no-op. The
    per-tool calls fired by the agent re-enter the hook with a
    concrete `tool_name`."""
    _install_lookup(monkeypatch, [])
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(tool_name=None, tool_input=None)
    # No exception.
    await hook(ctx)


async def test_hook_denies_when_tool_input_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool call with no `tool_input` metadata → fail-closed deny."""
    role_id = uuid4()
    _set_role_map(analyst=role_id)
    _install_lookup(monkeypatch, [])
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(tool_name="run_select")  # no tool_input
    with pytest.raises(PermissionDeniedError, match="structured input"):
        await hook(ctx)


async def test_hook_denies_when_tool_input_has_no_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_id = uuid4()
    _set_role_map(analyst=role_id)
    _install_lookup(monkeypatch, [])
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_select",
        tool_input={"columns": "*"},  # no table key
    )
    with pytest.raises(PermissionDeniedError, match="no table reference"):
        await hook(ctx)


# ===========================================================================
# Multi-table tools (run_join / run_aggregate)
# ===========================================================================


async def test_hook_denies_join_when_either_side_lacks_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_join: analyst can SELECT `payment` but NOT `customer`.
    The hook walks all references and denies on the first miss."""
    role_id = uuid4()
    _set_role_map(analyst=role_id)
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(
                role_id, DEFAULT_CONNECTION_ID, "public", "payment", "allow"
            ),
        ],
    )
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_join",
        tool_input={
            "left": "public.payment",
            "right": "public.customer",
            "join": {"table": "public.customer"},
        },
    )
    with pytest.raises(PermissionDeniedError, match="customer"):
        await hook(ctx)


async def test_hook_allows_join_when_both_sides_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_id = uuid4()
    _set_role_map(analyst=role_id)
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(
                role_id, DEFAULT_CONNECTION_ID, "public", "payment", "allow"
            ),
            _FakeRow(
                role_id, DEFAULT_CONNECTION_ID, "public", "customer", "allow"
            ),
        ],
    )
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_join",
        tool_input={
            "left": "public.payment",
            "right": "public.customer",
            "join": {"table": "public.customer"},
        },
    )
    await hook(ctx)


async def test_hook_walks_aggregate_joins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_aggregate: base_table + joins[i].table all checked."""
    role_id = uuid4()
    _set_role_map(analyst=role_id)
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(
                role_id, DEFAULT_CONNECTION_ID, "public", "payment", "allow"
            ),
        ],
    )
    resolver = DataPermissionResolver()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_FakeSessionFactory(),
        role_lookup=_stub_role_lookup,
    )
    ctx = _make_ctx(
        tool_name="run_aggregate",
        tool_input={
            "base_table": "public.payment",
            "joins": [{"table": "public.rental"}],
        },
    )
    with pytest.raises(PermissionDeniedError, match="rental"):
        await hook(ctx)
