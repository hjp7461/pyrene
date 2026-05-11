"""DataPermissionResolver — cache + decision-precedence unit tests.

Stubs the DB lookup so the cache + decision algorithm runs without
spinning up Postgres. DB integration is covered by
`tests/integration/test_resolver_db_data_rbac.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_data_rbac import permission_resolver as pr_module
from pyrene_data_rbac.permission_resolver import (
    DEFAULT_CONNECTION_ID,
    DataPermissionResolver,
)


class _FakeRow:
    """Duck type matching the attributes the resolver reads."""

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


class _FakeSession:
    """AsyncSession stand-in; never inspected."""


def _fake_session() -> AsyncSession:
    return cast(AsyncSession, _FakeSession())


def _install_lookup(
    monkeypatch: pytest.MonkeyPatch, rows: list[_FakeRow]
) -> list[tuple[tuple[UUID, ...], UUID]]:
    """Replace the repository lookup with an in-memory function.

    Returns a list capturing every `(role_ids, connection_id)` call so
    tests can assert cache hit/miss counts.
    """
    calls: list[tuple[tuple[UUID, ...], UUID]] = []

    async def _fake_lookup(
        session: Any,
        role_ids: Any,
        connection_id: UUID,
    ) -> list[_FakeRow]:
        role_id_tuple = tuple(role_ids)
        calls.append((role_id_tuple, connection_id))
        return [
            r
            for r in rows
            if r.role_id in role_id_tuple and r.connection_id == connection_id
        ]

    monkeypatch.setattr(
        pr_module, "list_permissions_for_roles_on_connection", _fake_lookup
    )
    return calls


# -------------------- Decision algorithm: tiers + deny-precedence -----------


CONN = DEFAULT_CONNECTION_ID


async def test_default_deny_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_lookup(monkeypatch, [])
    resolver = DataPermissionResolver()
    role_id = uuid4()
    decision = await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    assert decision is False


async def test_explicit_allow_grants_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_id = uuid4()
    _install_lookup(
        monkeypatch,
        [_FakeRow(role_id, CONN, "public", "payment", "allow")],
    )
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema="public",
            table="payment",
        )
        is True
    )


async def test_explicit_deny_blocks_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same (role, conn, schema, table) — deny wins."""
    role_id = uuid4()
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(role_id, CONN, "public", "payment", "allow"),
            _FakeRow(role_id, CONN, "public", "payment", "deny"),
        ],
    )
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema="public",
            table="payment",
        )
        is False
    )


async def test_schema_wildcard_allow_matches_any_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_id = uuid4()
    _install_lookup(
        monkeypatch,
        [_FakeRow(role_id, CONN, "public", "*", "allow")],
    )
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema="public",
            table="film",
        )
        is True
    )


async def test_full_wildcard_allow_matches_any_schema_any_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`(schema='*', table='*', allow)` — admin-equivalent on the connection."""
    role_id = uuid4()
    _install_lookup(
        monkeypatch,
        [_FakeRow(role_id, CONN, "*", "*", "allow")],
    )
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema="analytics",
            table="anything",
        )
        is True
    )


async def test_explicit_deny_beats_wildcard_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD-011 §위험 #3 (punch-out): wildcard allow + explicit deny on
    the same table — the explicit deny wins."""
    role_id = uuid4()
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(role_id, CONN, "public", "*", "allow"),
            _FakeRow(role_id, CONN, "public", "payment", "deny"),
        ],
    )
    resolver = DataPermissionResolver()
    # The explicit table is denied …
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema="public",
            table="payment",
        )
        is False
    )
    # … but other tables under the wildcard stay allowed.
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema="public",
            table="film",
        )
        is True
    )


async def test_explicit_allow_punches_through_wildcard_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverse: wildcard deny + explicit allow on one table → that
    table is allowed because the explicit tier wins."""
    role_id = uuid4()
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(role_id, CONN, "public", "*", "deny"),
            _FakeRow(role_id, CONN, "public", "film", "allow"),
        ],
    )
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema="public",
            table="film",
        )
        is True
    )
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema="public",
            table="payment",
        )
        is False
    )


async def test_union_of_roles_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One role allows, the other has no row → allow wins."""
    a, b = uuid4(), uuid4()
    _install_lookup(
        monkeypatch,
        [_FakeRow(a, CONN, "public", "payment", "allow")],
    )
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(a, b),
            connection_id=CONN,
            schema="public",
            table="payment",
        )
        is True
    )


async def test_deny_in_union_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """One role allows, the other denies → explicit deny wins."""
    a, b = uuid4(), uuid4()
    _install_lookup(
        monkeypatch,
        [
            _FakeRow(a, CONN, "public", "payment", "allow"),
            _FakeRow(b, CONN, "public", "payment", "deny"),
        ],
    )
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(a, b),
            connection_id=CONN,
            schema="public",
            table="payment",
        )
        is False
    )


async def test_empty_role_ids_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_lookup(monkeypatch, [])
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(),
            connection_id=CONN,
            schema="public",
            table="payment",
        )
        is False
    )
    # Short-circuited before DB.
    assert calls == []


async def test_resolver_normalizes_uppercase_and_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver lowercases / strips quotes before lookup so
    `PUBLIC.payment` and `"public"."payment"` collide on the same row.

    PM amend bypass cases 1-5 — the resolver is the last line of
    defense; the hook normalizes too, but the resolver re-normalizes
    so callers that bypass the hook still hit the canonical form.
    """
    role_id = uuid4()
    rows = [_FakeRow(role_id, CONN, "public", "payment", "allow")]
    _install_lookup(monkeypatch, rows)
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema="PUBLIC",
            table="PAYMENT",
        )
        is True
    )
    assert (
        await resolver.can_access(
            _fake_session(),
            role_ids=(role_id,),
            connection_id=CONN,
            schema='"public"',
            table='"payment"',
        )
        is True
    )


# -------------------- Cache behavior ----------------------------------------


async def test_cache_hit_on_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    role_id = uuid4()
    calls = _install_lookup(
        monkeypatch,
        [_FakeRow(role_id, CONN, "public", "payment", "allow")],
    )
    resolver = DataPermissionResolver()
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    assert len(calls) == 1


async def test_cache_isolates_by_connection_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different `connection_id` → distinct cache entries (PRD-011 §F2)."""
    role_id = uuid4()
    rows = [
        _FakeRow(
            role_id,
            UUID("11111111-1111-1111-1111-111111111111"),
            "public",
            "payment",
            "allow",
        ),
    ]
    calls = _install_lookup(monkeypatch, rows)
    resolver = DataPermissionResolver()
    conn_a = UUID("11111111-1111-1111-1111-111111111111")
    conn_b = UUID("22222222-2222-2222-2222-222222222222")
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=conn_a,
        schema="public",
        table="payment",
    )
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=conn_b,
        schema="public",
        table="payment",
    )
    assert len(calls) == 2


async def test_cache_key_sorts_role_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`(a, b)` and `(b, a)` hit the same cache entry."""
    a, b = uuid4(), uuid4()
    calls = _install_lookup(
        monkeypatch,
        [_FakeRow(a, CONN, "public", "payment", "allow")],
    )
    resolver = DataPermissionResolver()
    await resolver.can_access(
        _fake_session(),
        role_ids=(a, b),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    await resolver.can_access(
        _fake_session(),
        role_ids=(b, a),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    assert len(calls) == 1


async def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    role_id = uuid4()
    calls = _install_lookup(
        monkeypatch,
        [_FakeRow(role_id, CONN, "public", "payment", "allow")],
    )
    resolver = DataPermissionResolver(maxsize=16, ttl=0.05)
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    await asyncio.sleep(0.1)
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    assert len(calls) == 2


async def test_invalidate_role_drops_matching_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_a, role_b = uuid4(), uuid4()
    rows = [
        _FakeRow(role_a, CONN, "public", "payment", "allow"),
        _FakeRow(role_b, CONN, "public", "payment", "allow"),
    ]
    _install_lookup(monkeypatch, rows)
    resolver = DataPermissionResolver()
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_a,),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_b,),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    assert resolver._cache_size() == 2
    resolver.invalidate_role(role_a)
    assert resolver._cache_size() == 1
    remaining_role_ids = {k[0] for k in resolver._cache_keys()}
    assert remaining_role_ids == {(role_b,)}


async def test_invalidate_connection_drops_matching_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_id = uuid4()
    conn_a = UUID("11111111-1111-1111-1111-111111111111")
    conn_b = UUID("22222222-2222-2222-2222-222222222222")
    rows = [
        _FakeRow(role_id, conn_a, "public", "payment", "allow"),
        _FakeRow(role_id, conn_b, "public", "payment", "allow"),
    ]
    _install_lookup(monkeypatch, rows)
    resolver = DataPermissionResolver()
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=conn_a,
        schema="public",
        table="payment",
    )
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=conn_b,
        schema="public",
        table="payment",
    )
    assert resolver._cache_size() == 2
    resolver.invalidate_connection(conn_a)
    assert resolver._cache_size() == 1


async def test_invalidate_all_clears_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_id = uuid4()
    _install_lookup(
        monkeypatch,
        [_FakeRow(role_id, CONN, "public", "payment", "allow")],
    )
    resolver = DataPermissionResolver()
    await resolver.can_access(
        _fake_session(),
        role_ids=(role_id,),
        connection_id=CONN,
        schema="public",
        table="payment",
    )
    assert resolver._cache_size() == 1
    resolver.invalidate_all()
    assert resolver._cache_size() == 0


# -------------------- Default connection sentinel ---------------------------


def test_default_connection_id_is_phase1_uuid() -> None:
    """PLAN-011 sentinel matches `pyrene_sql.schema.models.DEFAULT_CONNECTION_ID`
    and the initdb default — every Phase 1 single-connection deployment
    routes through this UUID."""
    assert (
        UUID("00000000-0000-0000-0000-000000000001") == DEFAULT_CONNECTION_ID
    )
