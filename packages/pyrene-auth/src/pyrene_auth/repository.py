"""Thin data-access layer over the auth tables.

Keeps routing code free of inline `select()` boilerplate and gives unit
tests a seam to inject fakes. The functions are intentionally small.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.models import Role, Team, User, UserTeamRole


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(
        select(User).where(User.email == email, User.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def get_active_user_by_id(session: AsyncSession, user_id: UUID) -> User | None:
    result = await session.execute(
        select(User).where(
            User.id == user_id,
            User.deleted_at.is_(None),
            User.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def get_team_by_name(session: AsyncSession, name: str) -> Team | None:
    result = await session.execute(select(Team).where(Team.name == name))
    return result.scalar_one_or_none()


async def get_or_create_default_team(session: AsyncSession, name: str = "default") -> Team:
    existing = await get_team_by_name(session, name)
    if existing is not None:
        return existing
    team = Team(name=name)
    session.add(team)
    await session.flush()
    return team


async def get_role_by_name(session: AsyncSession, name: str) -> Role | None:
    result = await session.execute(select(Role).where(Role.name == name))
    return result.scalar_one_or_none()


async def list_user_roles_for_team(
    session: AsyncSession, user_id: UUID, team_id: UUID
) -> tuple[str, ...]:
    """Return role names granted to `user_id` within `team_id`, sorted for stability."""
    result = await session.execute(
        select(Role.name)
        .join(UserTeamRole, UserTeamRole.role_id == Role.id)
        .where(UserTeamRole.user_id == user_id, UserTeamRole.team_id == team_id)
        .order_by(Role.name)
    )
    return tuple(row[0] for row in result)


async def list_user_team_role_triples(
    session: AsyncSession, user_id: UUID
) -> list[tuple[UUID, str]]:
    """Return [(team_id, role_name), ...] for the user across all teams."""
    result = await session.execute(
        select(UserTeamRole.team_id, Role.name)
        .join(Role, Role.id == UserTeamRole.role_id)
        .where(UserTeamRole.user_id == user_id)
        .order_by(UserTeamRole.team_id, Role.name)
    )
    return [(row[0], row[1]) for row in result]


__all__ = [
    "get_active_user_by_id",
    "get_or_create_default_team",
    "get_role_by_name",
    "get_team_by_name",
    "get_user_by_email",
    "list_user_roles_for_team",
    "list_user_team_role_triples",
]
