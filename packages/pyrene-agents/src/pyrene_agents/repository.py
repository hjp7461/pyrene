"""Data-access helpers for AgentSpec / AgentVersion.

Thin layer over SQLAlchemy `select(...)` so route handlers stay readable.
All functions are async; the host app wires `AsyncSession` via the same
`set_session_dependency` hook that pyrene-auth uses.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_agents.models import AgentSpec, AgentVersion


async def get_spec_by_id(session: AsyncSession, spec_id: UUID) -> AgentSpec | None:
    """Return the spec (any team) or None."""
    result = await session.execute(select(AgentSpec).where(AgentSpec.id == spec_id))
    return result.scalar_one_or_none()


async def get_spec_for_team(
    session: AsyncSession, spec_id: UUID, team_id: UUID
) -> AgentSpec | None:
    """Team-scoped lookup. Returns None if spec is missing OR belongs to another team.

    Callers translate `None` into HTTP 404 (not 403) to avoid leaking
    enumeration data — PRD-008 §3.2 + RBAC enumeration defense.
    """
    result = await session.execute(
        select(AgentSpec).where(
            AgentSpec.id == spec_id, AgentSpec.team_id == team_id
        )
    )
    return result.scalar_one_or_none()


async def get_spec_by_name(
    session: AsyncSession, team_id: UUID, name: str
) -> AgentSpec | None:
    result = await session.execute(
        select(AgentSpec).where(
            AgentSpec.team_id == team_id, AgentSpec.name == name
        )
    )
    return result.scalar_one_or_none()


async def list_specs_for_team(
    session: AsyncSession, team_id: UUID
) -> Sequence[AgentSpec]:
    result = await session.execute(
        select(AgentSpec)
        .where(AgentSpec.team_id == team_id)
        .order_by(AgentSpec.name)
    )
    return result.scalars().all()


async def get_latest_version_number(
    session: AsyncSession, agent_id: UUID
) -> int:
    """Return the largest `version` for `agent_id`, or 0 if no versions exist."""
    result = await session.execute(
        select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent_id)
    )
    value = result.scalar_one_or_none()
    return int(value) if value is not None else 0


async def get_latest_version(
    session: AsyncSession, agent_id: UUID
) -> AgentVersion | None:
    """Return the highest-version row for `agent_id`, or None."""
    result = await session.execute(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_versions(
    session: AsyncSession, agent_id: UUID
) -> Sequence[AgentVersion]:
    result = await session.execute(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version)
    )
    return result.scalars().all()


__all__ = [
    "get_latest_version",
    "get_latest_version_number",
    "get_spec_by_id",
    "get_spec_by_name",
    "get_spec_for_team",
    "list_specs_for_team",
    "list_versions",
]
