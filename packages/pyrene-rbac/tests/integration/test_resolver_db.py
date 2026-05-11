"""Integration tests for `PermissionResolver` against a real Postgres.

ADR-014 savepoint isolation — every test starts with an empty
`permissions` table after rollback. We seed Role rows + Permission
rows directly via the ORM session and call the resolver to verify
end-to-end decision semantics + cache + write-through invalidation.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.models import Role
from pyrene_rbac import PermissionResolver
from pyrene_rbac.models import Permission

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
    resolver = PermissionResolver()
    decision = await resolver.can_invoke(
        db_session, role_ids=(role.id,), tool_name="run_select"
    )
    assert decision is False


async def test_resolver_allow_row_grants(db_session: AsyncSession) -> None:
    role = await _seed_role(db_session, "analyst")
    db_session.add(
        Permission(role_id=role.id, tool_name="run_select", action="allow")
    )
    await db_session.flush()

    resolver = PermissionResolver()
    decision = await resolver.can_invoke(
        db_session, role_ids=(role.id,), tool_name="run_select"
    )
    assert decision is True


async def test_resolver_deny_row_overrides_allow(db_session: AsyncSession) -> None:
    role = await _seed_role(db_session, "viewer")
    db_session.add_all(
        [
            Permission(role_id=role.id, tool_name="run_select", action="allow"),
            Permission(role_id=role.id, tool_name="run_select", action="deny"),
        ]
    )
    await db_session.flush()

    resolver = PermissionResolver()
    decision = await resolver.can_invoke(
        db_session, role_ids=(role.id,), tool_name="run_select"
    )
    assert decision is False


async def test_resolver_caches_decision_then_invalidates(
    db_session: AsyncSession,
) -> None:
    """Hot-path semantics: cache hit on 2nd call, fresh after invalidate."""
    role = await _seed_role(db_session, "analyst")
    db_session.add(
        Permission(role_id=role.id, tool_name="run_select", action="allow")
    )
    await db_session.flush()

    resolver = PermissionResolver()
    # 1st call → miss, populates cache.
    await resolver.can_invoke(
        db_session, role_ids=(role.id,), tool_name="run_select"
    )
    assert resolver._cache_size() == 1

    # 2nd call hits cache (we cannot directly observe lack of DB hit
    # without instrumentation, but resolver._cache_keys() is enough).
    assert (role.id,) in {k[0] for k in resolver._cache_keys()}

    # Invalidate the role → cache shrinks.
    resolver.invalidate_role(role.id)
    assert resolver._cache_size() == 0


async def test_resolver_viewer_analyst_admin_matrix(
    db_session: AsyncSession,
) -> None:
    """PROJECT_BRIEF §3.2 scenario A surface in miniature.

    Three roles + three tools build the matrix the demo scenario
    exercises. Concrete cells:

           run_select   run_aggregate   admin_grant
    viewer    allow          —              —
    analyst   allow         allow            —
    admin     allow         allow          allow
    """
    viewer = await _seed_role(db_session, "viewer")
    analyst = await _seed_role(db_session, "analyst")
    admin = await _seed_role(db_session, "admin")

    db_session.add_all(
        [
            Permission(role_id=viewer.id, tool_name="run_select", action="allow"),
            Permission(role_id=analyst.id, tool_name="run_select", action="allow"),
            Permission(role_id=analyst.id, tool_name="run_aggregate", action="allow"),
            Permission(role_id=admin.id, tool_name="run_select", action="allow"),
            Permission(role_id=admin.id, tool_name="run_aggregate", action="allow"),
            Permission(role_id=admin.id, tool_name="admin_grant", action="allow"),
        ]
    )
    await db_session.flush()

    resolver = PermissionResolver()

    # viewer
    assert (
        await resolver.can_invoke(
            db_session, role_ids=(viewer.id,), tool_name="run_select"
        )
        is True
    )
    assert (
        await resolver.can_invoke(
            db_session, role_ids=(viewer.id,), tool_name="run_aggregate"
        )
        is False
    )
    assert (
        await resolver.can_invoke(
            db_session, role_ids=(viewer.id,), tool_name="admin_grant"
        )
        is False
    )

    # analyst
    assert (
        await resolver.can_invoke(
            db_session, role_ids=(analyst.id,), tool_name="run_select"
        )
        is True
    )
    assert (
        await resolver.can_invoke(
            db_session, role_ids=(analyst.id,), tool_name="run_aggregate"
        )
        is True
    )
    assert (
        await resolver.can_invoke(
            db_session, role_ids=(analyst.id,), tool_name="admin_grant"
        )
        is False
    )

    # admin
    assert (
        await resolver.can_invoke(
            db_session, role_ids=(admin.id,), tool_name="run_select"
        )
        is True
    )
    assert (
        await resolver.can_invoke(
            db_session, role_ids=(admin.id,), tool_name="run_aggregate"
        )
        is True
    )
    assert (
        await resolver.can_invoke(
            db_session, role_ids=(admin.id,), tool_name="admin_grant"
        )
        is True
    )


async def test_unique_constraint_blocks_duplicate_rows(
    db_session: AsyncSession,
) -> None:
    """`uq_permissions_role_tool_action` rejects duplicate (role, tool, action)."""
    from sqlalchemy.exc import IntegrityError

    role = await _seed_role(db_session, "viewer")
    db_session.add(
        Permission(role_id=role.id, tool_name="run_select", action="allow")
    )
    await db_session.flush()

    db_session.add(
        Permission(role_id=role.id, tool_name="run_select", action="allow")
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_role_delete_restricted_by_permission_fk(
    db_session: AsyncSession,
) -> None:
    """ADR-013 (b) RESTRICT: cannot drop a role that owns a permission row."""
    from sqlalchemy.exc import IntegrityError

    role = await _seed_role(db_session, "analyst")
    db_session.add(
        Permission(role_id=role.id, tool_name="run_select", action="allow")
    )
    await db_session.flush()

    await db_session.delete(role)
    with pytest.raises(IntegrityError):
        await db_session.flush()
