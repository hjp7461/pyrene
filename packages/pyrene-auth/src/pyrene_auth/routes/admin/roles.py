"""Admin Role CRUD + UserTeamRole grant/revoke (PLAN-007 Day 3).

All endpoints require the caller to hold the `admin` role for their current
team. The `require_admin` dependency does the 403 enforcement; the SQL
layer doubles up with FK RESTRICT semantics for role deletion (ADR-013 (b)).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.dependencies import _session_proxy, require_admin
from pyrene_auth.models import Role, Team, User, UserTeamRole
from pyrene_auth.schemas import (
    RoleCreateRequest,
    RoleResponse,
    RoleUpdateRequest,
)
from pyrene_core import UserContext

admin_router = APIRouter(prefix="/admin", tags=["admin"])


async def _get_role_or_404(session: AsyncSession, role_id: UUID) -> Role:
    result = await session.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="role not found"
        )
    return role


@admin_router.get("/roles", response_model=list[RoleResponse])
async def list_roles(
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> list[RoleResponse]:
    result = await session.execute(select(Role).order_by(Role.name))
    return [
        RoleResponse(id=r.id, name=r.name, description=r.description)
        for r in result.scalars()
    ]


@admin_router.post(
    "/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED
)
async def create_role(
    body: RoleCreateRequest,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> RoleResponse:
    existing = await session.execute(select(Role).where(Role.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="role name already exists"
        )
    role = Role(name=body.name, description=body.description)
    session.add(role)
    await session.flush()
    await session.commit()
    return RoleResponse(id=role.id, name=role.name, description=role.description)


@admin_router.put("/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: UUID,
    body: RoleUpdateRequest,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> RoleResponse:
    role = await _get_role_or_404(session, role_id)
    role.description = body.description
    await session.flush()
    await session.commit()
    return RoleResponse(id=role.id, name=role.name, description=role.description)


@admin_router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: UUID,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> None:
    role = await _get_role_or_404(session, role_id)
    await session.delete(role)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="role is referenced (FK RESTRICT); revoke grants / permissions first",
        ) from exc


@admin_router.post(
    "/users/{user_id}/teams/{team_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def grant_role(
    user_id: UUID,
    team_id: UUID,
    role_id: UUID,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> None:
    user = await session.get(User, user_id)
    team = await session.get(Team, team_id)
    role = await session.get(Role, role_id)
    if user is None or team is None or role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="user, team, or role not found",
        )
    existing = await session.execute(
        select(UserTeamRole).where(
            UserTeamRole.user_id == user_id,
            UserTeamRole.team_id == team_id,
            UserTeamRole.role_id == role_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        # Idempotent grant — no-op + 204.
        return

    session.add(UserTeamRole(user_id=user_id, team_id=team_id, role_id=role_id))
    await session.commit()


@admin_router.delete(
    "/users/{user_id}/teams/{team_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_role(
    user_id: UUID,
    team_id: UUID,
    role_id: UUID,
    _: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> None:
    result = await session.execute(
        select(UserTeamRole).where(
            UserTeamRole.user_id == user_id,
            UserTeamRole.team_id == team_id,
            UserTeamRole.role_id == role_id,
        )
    )
    grant = result.scalar_one_or_none()
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="grant not found"
        )
    await session.delete(grant)
    await session.commit()


__all__ = ["admin_router"]
