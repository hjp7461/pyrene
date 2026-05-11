"""PermissionResolver — cache + decision-precedence unit tests.

These tests stub the DB lookup so they exercise the cache + decision
algorithm without spinning up Postgres. The DB integration path is
covered by `tests/integration/test_resolver_db.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_rbac import permission_resolver as pr_module
from pyrene_rbac.permission_resolver import (
    DEFAULT_CONNECTION_ID,
    PermissionResolver,
)


class _FakePermission:
    """Duck type matching the attributes the resolver reads."""

    def __init__(self, role_id: UUID, tool_name: str, action: str) -> None:
        self.role_id = role_id
        self.tool_name = tool_name
        self.action = action


class _FakeSession:
    """No-op AsyncSession stand-in — the resolver delegates the SQL
    work to `list_permissions_for_roles`, which we monkeypatch."""


def _fake_session() -> AsyncSession:
    """Bridge the `_FakeSession` duck type to the AsyncSession type
    the resolver expects. The body never inspects the session — the
    monkeypatched `list_permissions_for_roles` handles the SQL."""
    return cast(AsyncSession, _FakeSession())


def _install_lookup(
    monkeypatch: pytest.MonkeyPatch, rows: list[_FakePermission]
) -> list[tuple[tuple[UUID, ...], str]]:
    """Replace the repository lookup with an in-memory function.

    Returns a list that captures every `(role_ids, tool_name)` call so
    tests can assert cache hit/miss counts.
    """
    calls: list[tuple[tuple[UUID, ...], str]] = []

    async def _fake_lookup(
        session: Any, role_ids: Any, tool_name: str
    ) -> list[_FakePermission]:
        role_id_tuple = tuple(role_ids)
        calls.append((role_id_tuple, tool_name))
        matched = [
            r
            for r in rows
            if r.tool_name == tool_name and r.role_id in role_id_tuple
        ]
        return matched

    monkeypatch.setattr(pr_module, "list_permissions_for_roles", _fake_lookup)
    return calls


# -------------------- Decision algorithm --------------------


async def test_default_deny_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_lookup(monkeypatch, [])
    resolver = PermissionResolver()
    role_id = uuid4()
    decision = await resolver.can_invoke(
        _fake_session(), role_ids=(role_id,), tool_name="run_select"
    )
    assert decision is False


async def test_allow_row_grants_access(monkeypatch: pytest.MonkeyPatch) -> None:
    role_id = uuid4()
    _install_lookup(
        monkeypatch, [_FakePermission(role_id, "run_select", "allow")]
    )
    resolver = PermissionResolver()
    assert (
        await resolver.can_invoke(
            _fake_session(), role_ids=(role_id,), tool_name="run_select"
        )
        is True
    )


async def test_deny_precedence_over_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    """allow + deny on same (role, tool) → deny wins (PRD-010 §4)."""
    role_id = uuid4()
    _install_lookup(
        monkeypatch,
        [
            _FakePermission(role_id, "run_select", "allow"),
            _FakePermission(role_id, "run_select", "deny"),
        ],
    )
    resolver = PermissionResolver()
    assert (
        await resolver.can_invoke(
            _fake_session(), role_ids=(role_id,), tool_name="run_select"
        )
        is False
    )


async def test_union_of_roles_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """One role allows, the other has no row → allow wins."""
    a, b = uuid4(), uuid4()
    _install_lookup(
        monkeypatch,
        [_FakePermission(a, "run_select", "allow")],
    )
    resolver = PermissionResolver()
    assert (
        await resolver.can_invoke(
            _fake_session(), role_ids=(a, b), tool_name="run_select"
        )
        is True
    )


async def test_deny_in_union_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """One role allows, the other denies → deny wins."""
    a, b = uuid4(), uuid4()
    _install_lookup(
        monkeypatch,
        [
            _FakePermission(a, "run_select", "allow"),
            _FakePermission(b, "run_select", "deny"),
        ],
    )
    resolver = PermissionResolver()
    assert (
        await resolver.can_invoke(
            _fake_session(), role_ids=(a, b), tool_name="run_select"
        )
        is False
    )


async def test_empty_role_ids_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anonymous / role-less callers cannot invoke anything (default-deny)."""
    calls = _install_lookup(monkeypatch, [])
    resolver = PermissionResolver()
    assert (
        await resolver.can_invoke(
            _fake_session(), role_ids=(), tool_name="run_select"
        )
        is False
    )
    # Short-circuited before DB.
    assert calls == []


# -------------------- Cache behavior --------------------


async def test_cache_hit_on_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    role_id = uuid4()
    calls = _install_lookup(
        monkeypatch, [_FakePermission(role_id, "run_select", "allow")]
    )
    resolver = PermissionResolver()
    await resolver.can_invoke(
        _fake_session(), role_ids=(role_id,), tool_name="run_select"
    )
    await resolver.can_invoke(
        _fake_session(), role_ids=(role_id,), tool_name="run_select"
    )
    # Second call serves from cache → only one DB lookup.
    assert len(calls) == 1


async def test_cache_key_normalizes_tool_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case + whitespace variants collapse to the same cache entry."""
    role_id = uuid4()
    calls = _install_lookup(
        monkeypatch, [_FakePermission(role_id, "run_select", "allow")]
    )
    resolver = PermissionResolver()
    await resolver.can_invoke(
        _fake_session(), role_ids=(role_id,), tool_name="run_select"
    )
    await resolver.can_invoke(
        _fake_session(), role_ids=(role_id,), tool_name="Run_Select"
    )
    await resolver.can_invoke(
        _fake_session(), role_ids=(role_id,), tool_name="  run_select  "
    )
    assert len(calls) == 1
    # DB sees the normalized form.
    assert calls[0][1] == "run_select"


async def test_cache_key_sorts_role_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """`(a, b)` and `(b, a)` should hit the same cache entry."""
    a, b = uuid4(), uuid4()
    calls = _install_lookup(
        monkeypatch, [_FakePermission(a, "run_select", "allow")]
    )
    resolver = PermissionResolver()
    await resolver.can_invoke(
        _fake_session(), role_ids=(a, b), tool_name="run_select"
    )
    await resolver.can_invoke(
        _fake_session(), role_ids=(b, a), tool_name="run_select"
    )
    assert len(calls) == 1


async def test_cache_isolates_by_connection_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different `connection_id` → distinct cache entries (multi-tenant)."""
    role_id = uuid4()
    calls = _install_lookup(
        monkeypatch, [_FakePermission(role_id, "run_select", "allow")]
    )
    resolver = PermissionResolver()
    conn_a = UUID("11111111-1111-1111-1111-111111111111")
    conn_b = UUID("22222222-2222-2222-2222-222222222222")
    await resolver.can_invoke(
        _fake_session(),
        role_ids=(role_id,),
        tool_name="run_select",
        connection_id=conn_a,
    )
    await resolver.can_invoke(
        _fake_session(),
        role_ids=(role_id,),
        tool_name="run_select",
        connection_id=conn_b,
    )
    assert len(calls) == 2


# -------------------- TTL expiry + invalidation --------------------


async def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    role_id = uuid4()
    calls = _install_lookup(
        monkeypatch, [_FakePermission(role_id, "run_select", "allow")]
    )
    # Tiny TTL → second call after sleep falls out of cache.
    resolver = PermissionResolver(maxsize=16, ttl=0.05)
    await resolver.can_invoke(
        _fake_session(), role_ids=(role_id,), tool_name="run_select"
    )
    await asyncio.sleep(0.1)
    await resolver.can_invoke(
        _fake_session(), role_ids=(role_id,), tool_name="run_select"
    )
    assert len(calls) == 2


async def test_invalidate_role_drops_matching_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_a, role_b = uuid4(), uuid4()
    rows = [
        _FakePermission(role_a, "run_select", "allow"),
        _FakePermission(role_b, "run_select", "allow"),
    ]
    _install_lookup(monkeypatch, rows)
    resolver = PermissionResolver()

    await resolver.can_invoke(
        _fake_session(), role_ids=(role_a,), tool_name="run_select"
    )
    await resolver.can_invoke(
        _fake_session(), role_ids=(role_b,), tool_name="run_select"
    )
    assert resolver._cache_size() == 2

    # Invalidate role_a only — role_b entry stays.
    resolver.invalidate_role(role_a)
    assert resolver._cache_size() == 1
    remaining_role_ids = {k[0] for k in resolver._cache_keys()}
    assert remaining_role_ids == {(role_b,)}


async def test_invalidate_all_clears_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_id = uuid4()
    _install_lookup(
        monkeypatch, [_FakePermission(role_id, "run_select", "allow")]
    )
    resolver = PermissionResolver()
    await resolver.can_invoke(
        _fake_session(), role_ids=(role_id,), tool_name="run_select"
    )
    assert resolver._cache_size() == 1
    resolver.invalidate_all()
    assert resolver._cache_size() == 0


async def test_invalidate_role_affects_unions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compound role keys containing the invalidated role drop too."""
    role_a, role_b = uuid4(), uuid4()
    _install_lookup(
        monkeypatch, [_FakePermission(role_a, "run_select", "allow")]
    )
    resolver = PermissionResolver()
    await resolver.can_invoke(
        _fake_session(),
        role_ids=(role_a, role_b),
        tool_name="run_select",
    )
    assert resolver._cache_size() == 1
    resolver.invalidate_role(role_b)  # role_b appears in the compound key
    assert resolver._cache_size() == 0


# -------------------- Default connection sentinel --------------------


def test_default_connection_id_is_nil_uuid() -> None:
    """Phase 2 sentinel: PLAN-011 will swap to a real connection_id."""
    assert UUID("00000000-0000-0000-0000-000000000000") == DEFAULT_CONNECTION_ID
