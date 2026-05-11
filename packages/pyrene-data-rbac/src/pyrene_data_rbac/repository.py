"""Data-access layer for `data_permissions` rows.

Thin functions over SQLAlchemy `select()` so the route handler stays
declarative and unit tests can substitute a fake session. The resolver
uses `list_permissions_for_roles_on_connection` for its DB lookup path.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_data_rbac.models import DataPermission


async def get_data_permission_by_id(
    session: AsyncSession, permission_id: UUID
) -> DataPermission | None:
    """Lookup by PK — used by the CRUD endpoints (PUT / DELETE)."""
    result = await session.execute(
        select(DataPermission).where(DataPermission.id == permission_id)
    )
    return result.scalar_one_or_none()


async def list_data_permissions(session: AsyncSession) -> list[DataPermission]:
    """Full matrix dump. Admin-only endpoint, not a hot path."""
    result = await session.execute(
        select(DataPermission).order_by(
            DataPermission.connection_id,
            DataPermission.schema,
            DataPermission.table_name,
            DataPermission.role_id,
            DataPermission.action,
        )
    )
    return list(result.scalars().all())


async def list_permissions_for_roles_on_connection(
    session: AsyncSession,
    role_ids: Iterable[UUID],
    connection_id: UUID,
) -> list[DataPermission]:
    """Hot path: every row scoped to `(role_id IN ..., connection_id = ?)`.

    The resolver pulls the **full set** for the connection (including
    wildcards) and then evaluates explicit > wildcard with deny-wins in
    Python. Returning rows scoped to a single connection keeps the
    matrix per connection bounded — Phase 2 expects ≤ a few hundred
    rows per role/connection, so loading them all on a cache miss is
    cheaper than emitting one query per `(schema, table)` candidate.

    Index `ix_data_permissions_role_conn_schema_table` covers
    `(role_id, connection_id, ...)`; the planner uses an Index Scan +
    bitmap-OR when `role_id IN (..)` has multiple values.
    """
    role_id_list = list(role_ids)
    if not role_id_list:
        return []
    result = await session.execute(
        select(DataPermission)
        .where(
            DataPermission.role_id.in_(role_id_list),
            DataPermission.connection_id == connection_id,
        )
        # Order so deny rows come last → the in-Python evaluation
        # can short-circuit on the first deny matching the lookup.
        .order_by(DataPermission.action)
    )
    return list(result.scalars().all())


__all__ = [
    "get_data_permission_by_id",
    "list_data_permissions",
    "list_permissions_for_roles_on_connection",
]
