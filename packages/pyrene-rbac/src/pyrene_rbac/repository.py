"""Data-access layer for `permissions` rows.

Thin functions over SQLAlchemy `select()` so the route handler stays
declarative and unit tests can substitute a fake session. The resolver
(`permission_resolver.py`) uses these directly for its DB lookup path.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.models import Role
from pyrene_rbac.models import Permission


async def get_permission_by_id(
    session: AsyncSession, permission_id: UUID
) -> Permission | None:
    result = await session.execute(
        select(Permission).where(Permission.id == permission_id)
    )
    return result.scalar_one_or_none()


async def list_permissions(session: AsyncSession) -> list[Permission]:
    """Full matrix dump. Admin-only endpoint, not a hot path."""
    result = await session.execute(
        select(Permission).order_by(
            Permission.tool_name, Permission.role_id, Permission.action
        )
    )
    return list(result.scalars().all())


async def list_permissions_for_roles(
    session: AsyncSession, role_ids: Iterable[UUID], tool_name: str
) -> list[Permission]:
    """Hot path: return every row matching `(role_id IN ..., tool_name = ?)`.

    The resolver hits this once per cache miss. The `(tool_name,
    role_id)` index in `0004_rbac_matrix.py` covers it; ordering by
    action keeps deny rows last so the caller can short-circuit on
    deny without an extra pass.
    """
    role_id_list = list(role_ids)
    if not role_id_list:
        return []
    result = await session.execute(
        select(Permission)
        .where(
            Permission.tool_name == tool_name,
            Permission.role_id.in_(role_id_list),
        )
        .order_by(Permission.action)
    )
    return list(result.scalars().all())


async def list_roles_by_id(
    session: AsyncSession, role_ids: Iterable[UUID]
) -> list[Role]:
    """Resolve role_id -> Role rows. Used by the matrix endpoint to
    label rows with role.name in the response."""
    role_id_list = list(role_ids)
    if not role_id_list:
        return []
    result = await session.execute(
        select(Role).where(Role.id.in_(role_id_list)).order_by(Role.name)
    )
    return list(result.scalars().all())


async def list_all_roles(session: AsyncSession) -> list[Role]:
    """Every role, alphabetical. Used by `GET /rbac/matrix` to render
    the row axis even for roles that hold zero permissions."""
    result = await session.execute(select(Role).order_by(Role.name))
    return list(result.scalars().all())


__all__ = [
    "get_permission_by_id",
    "list_all_roles",
    "list_permissions",
    "list_permissions_for_roles",
    "list_roles_by_id",
]
