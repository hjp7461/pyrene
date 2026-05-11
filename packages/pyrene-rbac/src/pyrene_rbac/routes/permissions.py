"""Admin-only RBAC matrix CRUD + matrix view.

PLAN-010 Day 1 surface:

```
GET    /rbac/permissions             -> list every row
POST   /rbac/permissions             -> create (201)
PUT    /rbac/permissions/{id}        -> mutate action
DELETE /rbac/permissions/{id}        -> 204
GET    /rbac/matrix                  -> Role x Tool 2D snapshot
```

Every endpoint is gated by `require_admin` (PLAN-007). Mutating
endpoints call `resolver.invalidate_role(role_id)` AFTER the commit
returns (ADR-008 §3 — commit-before-invalidate so rollback leaves the
cache in the stale-but-correct state).

The `set_resolver(...)` module hook is the wiring point: the host
app constructs one `PermissionResolver` instance and registers it
here, then `register_hooks(gateway, resolver=that_same_one, ...)`
attaches the read path. One shared resolver across the read + write
sides is what makes write-through invalidation work.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.dependencies import _session_proxy, require_admin
from pyrene_core import UserContext
from pyrene_rbac.models import Permission
from pyrene_rbac.permission_resolver import PermissionResolver
from pyrene_rbac.repository import (
    get_permission_by_id,
    list_all_roles,
    list_permissions,
)
from pyrene_rbac.schemas import (
    MatrixResponse,
    MatrixRoleEntry,
    PermissionAction,
    PermissionCreateRequest,
    PermissionResponse,
    PermissionUpdateRequest,
)


def _missing_resolver() -> PermissionResolver:  # pragma: no cover
    raise RuntimeError(
        "pyrene_rbac.routes.permissions resolver is not configured. "
        "Call `set_resolver(...)` at app startup."
    )


# Module slot: either a sentinel callable raising on access, or the
# registered `PermissionResolver` instance. Tests / app startup swap via
# `set_resolver(...)` / `reset_resolver()`.
_resolver_factory: PermissionResolver | object = _missing_resolver


def set_resolver(resolver: PermissionResolver) -> None:
    """Register the shared resolver instance.

    The route handlers reach the resolver via `_get_resolver()` rather
    than a FastAPI Depends() because the resolver lives for the
    process lifetime, not the request scope.
    """
    global _resolver_factory
    _resolver_factory = resolver


def reset_resolver() -> None:
    """Test seam — clear the resolver wiring between tests."""
    global _resolver_factory
    _resolver_factory = _missing_resolver


def _get_resolver() -> PermissionResolver:
    if isinstance(_resolver_factory, PermissionResolver):
        return _resolver_factory
    raise RuntimeError(
        "pyrene_rbac resolver is not configured. "
        "Call `set_resolver(...)` at app startup."
    )


permissions_router = APIRouter(prefix="/rbac", tags=["rbac"])


def _to_response(p: Permission) -> PermissionResponse:
    # Action column is stored as `String(8)` but constrained to the
    # `PermissionAction` literal at the schema layer; the cast here
    # mirrors the gateway pattern for `transport` (servers.py).
    action: PermissionAction = "deny" if p.action == "deny" else "allow"
    return PermissionResponse(
        id=p.id,
        role_id=p.role_id,
        tool_name=p.tool_name,
        action=action,
        created_at=p.created_at,
    )


# -------------------- CRUD --------------------


@permissions_router.get("/permissions", response_model=list[PermissionResponse])
async def list_permissions_endpoint(
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> list[PermissionResponse]:
    rows = await list_permissions(session)
    return [_to_response(r) for r in rows]


@permissions_router.post(
    "/permissions",
    response_model=PermissionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_permission_endpoint(
    body: PermissionCreateRequest,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> PermissionResponse:
    permission = Permission(
        role_id=body.role_id,
        tool_name=body.tool_name.strip().lower(),
        action=body.action,
    )
    session.add(permission)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Two flavors: (a) duplicate (UNIQUE), (b) bad FK (role_id).
        # Both surface as 409 — the client must inspect to disambiguate;
        # the detail message hints at the most likely cause.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "permission already exists for this (role, tool, action) "
                "or role_id does not reference a known role"
            ),
        ) from exc

    await session.refresh(permission)
    # ADR-008 §3: invalidate AFTER commit so a rollback leaves the
    # cache in the (stale-but-correct) pre-write state.
    _get_resolver().invalidate_role(body.role_id)
    return _to_response(permission)


@permissions_router.put(
    "/permissions/{permission_id}", response_model=PermissionResponse
)
async def update_permission_endpoint(
    permission_id: UUID,
    body: PermissionUpdateRequest,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> PermissionResponse:
    permission = await get_permission_by_id(session, permission_id)
    if permission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="permission not found"
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


@permissions_router.delete(
    "/permissions/{permission_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_permission_endpoint(
    permission_id: UUID,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> None:
    permission = await get_permission_by_id(session, permission_id)
    if permission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="permission not found"
        )
    role_id = permission.role_id
    await session.delete(permission)
    await session.commit()
    _get_resolver().invalidate_role(role_id)


# -------------------- Matrix view --------------------


@permissions_router.get("/matrix", response_model=MatrixResponse)
async def get_matrix_endpoint(
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> MatrixResponse:
    """Return the Role x Tool 2D matrix.

    Shape: every role (even with zero permissions) is rendered as a
    row so the admin UI can show empty cells; the column axis is the
    distinct set of `tool_name`s that appear in `permissions`. Tools
    discovered by PLAN-009 but with no row in `permissions` are
    deny-by-default and intentionally NOT listed here — the matrix
    surfaces the explicit policy only. The UI joins with
    `mcp_tools.name` for the full column axis.
    """
    roles = await list_all_roles(session)
    permissions = await list_permissions(session)

    by_role: dict[UUID, dict[str, PermissionAction]] = {r.id: {} for r in roles}
    tools_seen: set[str] = set()
    for p in permissions:
        tools_seen.add(p.tool_name)
        action: PermissionAction = "deny" if p.action == "deny" else "allow"
        # When both allow + deny exist for the same (role, tool) the
        # resolver picks deny; mirror that here so the matrix UI
        # shows what the resolver would actually decide.
        cell = by_role.setdefault(p.role_id, {})
        existing = cell.get(p.tool_name)
        if existing == "deny":
            continue
        cell[p.tool_name] = action

    entries = [
        MatrixRoleEntry(
            role_id=r.id, role_name=r.name, tools=by_role.get(r.id, {})
        )
        for r in roles
    ]
    return MatrixResponse(roles=entries, tools=sorted(tools_seen))


__all__ = [
    "permissions_router",
    "reset_resolver",
    "set_resolver",
]
