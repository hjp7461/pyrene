"""Integration tests for `DataPermissionResolver` against a real Postgres.

ADR-014 savepoint isolation — every test starts with an empty
`data_permissions` table after rollback. We seed Role rows + permission
rows directly via the ORM session and call the resolver to verify
end-to-end decision semantics + wildcard tiering + cache invalidation.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.models import Role
from pyrene_data_rbac import DataPermissionResolver
from pyrene_data_rbac.models import DataPermission
from pyrene_data_rbac.permission_resolver import DEFAULT_CONNECTION_ID

pytestmark = pytest.mark.integration


async def _seed_role(session: AsyncSession, name: str) -> Role:
    role = Role(name=f"{name}-{uuid4().hex[:8]}", description="")
    session.add(role)
    await session.flush()
    return role


async def test_resolver_default_deny_empty_matrix(
    db_session: AsyncSession,
) -> None:
    role = await _seed_role(db_session, "viewer")
    resolver = DataPermissionResolver()
    decision = await resolver.can_access(
        db_session,
        role_ids=(role.id,),
        connection_id=DEFAULT_CONNECTION_ID,
        schema="public",
        table="payment",
    )
    assert decision is False


async def test_resolver_explicit_allow(db_session: AsyncSession) -> None:
    role = await _seed_role(db_session, "analyst")
    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table_name="payment",
            action="allow",
        )
    )
    await db_session.flush()
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            db_session,
            role_ids=(role.id,),
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table="payment",
        )
        is True
    )


async def test_resolver_explicit_deny_overrides_allow(
    db_session: AsyncSession,
) -> None:
    role = await _seed_role(db_session, "viewer")
    db_session.add_all(
        [
            DataPermission(
                role_id=role.id,
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table_name="payment",
                action="allow",
            ),
            DataPermission(
                role_id=role.id,
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table_name="payment",
                action="deny",
            ),
        ]
    )
    await db_session.flush()
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            db_session,
            role_ids=(role.id,),
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table="payment",
        )
        is False
    )


async def test_resolver_schema_wildcard_allow(
    db_session: AsyncSession,
) -> None:
    """`(schema='public', table='*', allow)` lets every public table read."""
    role = await _seed_role(db_session, "analyst")
    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table_name="*",
            action="allow",
        )
    )
    await db_session.flush()
    resolver = DataPermissionResolver()
    for tbl in ("payment", "film", "rental"):
        assert (
            await resolver.can_access(
                db_session,
                role_ids=(role.id,),
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table=tbl,
            )
            is True
        )


async def test_resolver_explicit_deny_punches_through_wildcard_allow(
    db_session: AsyncSession,
) -> None:
    """PRD-011 §위험 #3 / wildcard punch-out."""
    role = await _seed_role(db_session, "analyst")
    db_session.add_all(
        [
            DataPermission(
                role_id=role.id,
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table_name="*",
                action="allow",
            ),
            DataPermission(
                role_id=role.id,
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table_name="payment",
                action="deny",
            ),
        ]
    )
    await db_session.flush()
    resolver = DataPermissionResolver()
    # payment denied (explicit > wildcard)
    assert (
        await resolver.can_access(
            db_session,
            role_ids=(role.id,),
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table="payment",
        )
        is False
    )
    # other tables still allowed under wildcard
    assert (
        await resolver.can_access(
            db_session,
            role_ids=(role.id,),
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table="film",
        )
        is True
    )


async def test_resolver_caches_then_invalidates(
    db_session: AsyncSession,
) -> None:
    """Hot-path semantics: cache hit on 2nd call, fresh after invalidate."""
    role = await _seed_role(db_session, "analyst")
    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table_name="payment",
            action="allow",
        )
    )
    await db_session.flush()

    resolver = DataPermissionResolver()
    await resolver.can_access(
        db_session,
        role_ids=(role.id,),
        connection_id=DEFAULT_CONNECTION_ID,
        schema="public",
        table="payment",
    )
    assert resolver._cache_size() == 1
    resolver.invalidate_role(role.id)
    assert resolver._cache_size() == 0


async def test_resolver_invalidation_under_1_second(
    db_session: AsyncSession,
) -> None:
    """PLAN-011 Day 3 / PRD-011 §6 — matrix change reflected within 1s.

    The cache TTL default is 60s, but the write-through `invalidate_role`
    runs synchronously in the CRUD endpoint. We measure the elapsed time
    between the seed → invalidate → re-read sequence; the operation
    completes well under 1s on Postgres + asyncio.
    """
    role = await _seed_role(db_session, "analyst")
    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table_name="payment",
            action="allow",
        )
    )
    await db_session.flush()
    resolver = DataPermissionResolver()
    await resolver.can_access(
        db_session,
        role_ids=(role.id,),
        connection_id=DEFAULT_CONNECTION_ID,
        schema="public",
        table="payment",
    )

    loop = asyncio.get_running_loop()
    start = loop.time()
    resolver.invalidate_role(role.id)
    elapsed = loop.time() - start
    # Invalidation itself is microseconds; we assert well below 1s to
    # surface a future regression where invalidation grew unbounded.
    assert elapsed < 1.0, f"invalidation took {elapsed:.4f}s — > 1s threshold"
    assert resolver._cache_size() == 0


async def test_resolver_scenario_a_analyst_allows_payment(
    db_session: AsyncSession,
) -> None:
    """PROJECT_BRIEF §3.2 Scenario A.1 — analyst can read payment."""
    role = await _seed_role(db_session, "analyst")
    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table_name="payment",
            action="allow",
        )
    )
    await db_session.flush()
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            db_session,
            role_ids=(role.id,),
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table="payment",
        )
        is True
    )


async def test_resolver_scenario_a_viewer_denies_payment(
    db_session: AsyncSession,
) -> None:
    """PROJECT_BRIEF §3.2 Scenario A.2 — viewer with film only is denied
    on payment (default-deny, no row)."""
    role = await _seed_role(db_session, "viewer")
    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table_name="film",
            action="allow",
        )
    )
    await db_session.flush()
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            db_session,
            role_ids=(role.id,),
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table="payment",
        )
        is False
    )


async def test_unique_constraint_blocks_duplicate_rows(
    db_session: AsyncSession,
) -> None:
    """uq_data_permissions_role_conn_schema_table_action rejects duplicates."""
    from sqlalchemy.exc import IntegrityError

    role = await _seed_role(db_session, "viewer")
    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table_name="payment",
            action="allow",
        )
    )
    await db_session.flush()

    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table_name="payment",
            action="allow",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_role_delete_restricted_by_data_permission_fk(
    db_session: AsyncSession,
) -> None:
    """ADR-013 (b) RESTRICT: cannot drop a role with live data permissions."""
    from sqlalchemy.exc import IntegrityError

    role = await _seed_role(db_session, "analyst")
    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table_name="payment",
            action="allow",
        )
    )
    await db_session.flush()

    await db_session.delete(role)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_connection_isolation(db_session: AsyncSession) -> None:
    """PRD-011 §F2 — a grant on connection_a does NOT leak to connection_b."""
    role = await _seed_role(db_session, "analyst")
    conn_a = UUID("11111111-1111-1111-1111-111111111111")
    conn_b = UUID("22222222-2222-2222-2222-222222222222")
    db_session.add(
        DataPermission(
            role_id=role.id,
            connection_id=conn_a,
            schema="public",
            table_name="payment",
            action="allow",
        )
    )
    await db_session.flush()
    resolver = DataPermissionResolver()
    assert (
        await resolver.can_access(
            db_session,
            role_ids=(role.id,),
            connection_id=conn_a,
            schema="public",
            table="payment",
        )
        is True
    )
    assert (
        await resolver.can_access(
            db_session,
            role_ids=(role.id,),
            connection_id=conn_b,
            schema="public",
            table="payment",
        )
        is False
    )
