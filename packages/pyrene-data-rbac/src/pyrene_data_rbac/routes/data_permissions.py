"""Admin-only data-RBAC matrix CRUD.

PLAN-011 Day 1 surface:

```
GET    /rbac/data-permissions             -> list every row
POST   /rbac/data-permissions             -> create (201)
PUT    /rbac/data-permissions/{id}        -> mutate action
DELETE /rbac/data-permissions/{id}        -> 204
```

Every endpoint is gated by `require_admin` (PLAN-007). Mutating
endpoints call `resolver.invalidate_role(role_id)` AFTER the commit
returns (ADR-008 §3 — commit-before-invalidate so rollback leaves
the cache in the stale-but-correct state).

`set_resolver(...)` is the module hook the host app calls at startup
to wire one shared `DataPermissionResolver` into both the read
(`hooks.make_data_rbac_hook`) and write (these endpoints) paths so
write-through invalidation works.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.dependencies import _session_proxy, require_admin
from pyrene_core import UserContext
from pyrene_data_rbac.models import DataPermission
from pyrene_data_rbac.permission_resolver import DataPermissionResolver
from pyrene_data_rbac.repository import (
    get_data_permission_by_id,
    list_data_permissions,
)
from pyrene_data_rbac.schemas import (
    DataPermissionAction,
    DataPermissionCreateRequest,
    DataPermissionResponse,
    DataPermissionUpdateRequest,
)


def _missing_resolver() -> DataPermissionResolver:  # pragma: no cover
    raise RuntimeError(
        "pyrene_data_rbac.routes.data_permissions resolver is not configured. "
        "Call `set_resolver(...)` at app startup."
    )


# Module slot: either a sentinel callable raising on access, or the
# registered `DataPermissionResolver` instance. Tests / app startup
# swap via `set_resolver(...)` / `reset_resolver()`.
_resolver_factory: DataPermissionResolver | object = _missing_resolver


def set_resolver(resolver: DataPermissionResolver) -> None:
    """Register the shared resolver instance.

    The route handlers reach the resolver via `_get_resolver()` rather
    than FastAPI Depends() because the resolver lives for the process
    lifetime, not the request scope.
    """
    global _resolver_factory
    _resolver_factory = resolver


def reset_resolver() -> None:
    """Test seam — clear the resolver wiring between tests."""
    global _resolver_factory
    _resolver_factory = _missing_resolver


def _get_resolver() -> DataPermissionResolver:
    if isinstance(_resolver_factory, DataPermissionResolver):
        return _resolver_factory
    raise RuntimeError(
        "pyrene_data_rbac resolver is not configured. "
        "Call `set_resolver(...)` at app startup."
    )


data_permissions_router = APIRouter(prefix="/rbac", tags=["rbac"])


def _to_response(p: DataPermission) -> DataPermissionResponse:
    action: DataPermissionAction = "deny" if p.action == "deny" else "allow"
    return DataPermissionResponse(
        id=p.id,
        role_id=p.role_id,
        connection_id=p.connection_id,
        schema=p.schema,
        table=p.table_name,
        action=action,
        created_at=p.created_at,
    )


# -------------------- CRUD --------------------


@data_permissions_router.get(
    "/data-permissions", response_model=list[DataPermissionResponse]
)
async def list_data_permissions_endpoint(
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> list[DataPermissionResponse]:
    rows = await list_data_permissions(session)
    return [_to_response(r) for r in rows]


@data_permissions_router.post(
    "/data-permissions",
    response_model=DataPermissionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_permission_endpoint(
    body: DataPermissionCreateRequest,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> DataPermissionResponse:
    permission = DataPermission(
        role_id=body.role_id,
        connection_id=body.connection_id,
        schema=body.schema,
        table_name=body.table,
        action=body.action,
    )
    session.add(permission)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Two flavours: (a) duplicate row (UNIQUE), (b) bad FK
        # (role_id not in `roles`). Both surface as 409 — the client
        # must inspect to disambiguate.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "data permission already exists for this "
                "(role, connection, schema, table, action) "
                "or role_id does not reference a known role"
            ),
        ) from exc

    await session.refresh(permission)
    # ADR-008 §3: invalidate AFTER commit so a rollback leaves the
    # cache in the (stale-but-correct) pre-write state.
    _get_resolver().invalidate_role(body.role_id)
    return _to_response(permission)


@data_permissions_router.put(
    "/data-permissions/{permission_id}",
    response_model=DataPermissionResponse,
)
async def update_data_permission_endpoint(
    permission_id: UUID,
    body: DataPermissionUpdateRequest,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> DataPermissionResponse:
    permission = await get_data_permission_by_id(session, permission_id)
    if permission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="data permission not found",
        )
    permission.action = body.action
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="flipping action would collide with an existing row",
        ) from exc
    await session.refresh(permission)
    _get_resolver().invalidate_role(permission.role_id)
    return _to_response(permission)


@data_permissions_router.delete(
    "/data-permissions/{permission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_data_permission_endpoint(
    permission_id: UUID,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> None:
    permission = await get_data_permission_by_id(session, permission_id)
    if permission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="data permission not found",
        )
    role_id = permission.role_id
    await session.delete(permission)
    await session.commit()
    _get_resolver().invalidate_role(role_id)


__all__ = [
    "data_permissions_router",
    "reset_resolver",
    "set_resolver",
]
