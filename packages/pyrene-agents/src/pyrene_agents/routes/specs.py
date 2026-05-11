"""AgentSpec / AgentVersion CRUD endpoints.

Enumeration-defense convention (matches pyrene-auth):
  - Other-team spec_id → 404 (not 403). Admins can see all teams' specs.
  - Bad output_schema_key body → 422 (Pydantic ValidationError on Literal).
  - Missing tool name → builder rejects at run time; spec creation itself
    does not validate tools (PRD-009 / PLAN-009 will eventually own that).

`POST /agents/specs` and `POST /agents/specs/{id}/versions` require admin
role. Reads (GET) require any authenticated user with a team match.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_agents.models import AgentSpec, AgentVersion
from pyrene_agents.repository import (
    get_latest_version,
    get_latest_version_number,
    get_spec_by_name,
    get_spec_for_team,
    list_specs_for_team,
    list_versions,
)
from pyrene_agents.schemas import (
    AgentSpecCreate,
    AgentSpecResponse,
    AgentVersionCreate,
    AgentVersionResponse,
)
from pyrene_auth.dependencies import (
    _session_proxy,
    get_current_user,
    require_admin,
)
from pyrene_core import UserContext

specs_router = APIRouter(prefix="/agents/specs", tags=["agents"])


async def _resolve_latest_version_number(
    session: AsyncSession, agent_id: UUID
) -> int:
    return await get_latest_version_number(session, agent_id)


def _spec_to_response(spec: AgentSpec, latest: int) -> AgentSpecResponse:
    return AgentSpecResponse(
        id=spec.id,
        name=spec.name,
        team_id=spec.team_id,
        description=spec.description,
        created_by=spec.created_by,
        created_at=spec.created_at,
        latest_version=latest,
    )


def _version_to_response(version: AgentVersion) -> AgentVersionResponse:
    return AgentVersionResponse(
        id=version.id,
        agent_id=version.agent_id,
        version=version.version,
        output_schema_key=version.output_schema_key,
        system_prompt=version.system_prompt,
        tools=tuple(version.tools),
        created_by=version.created_by,
        created_at=version.created_at,
        published_at=version.published_at,
    )


@specs_router.post(
    "",
    response_model=AgentSpecResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_spec(
    body: AgentSpecCreate,
    current: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> AgentSpecResponse:
    """Create an AgentSpec + AgentVersion v1 atomically.

    The (team_id, name) tuple is unique; 409 on conflict.
    """
    existing = await get_spec_by_name(session, current.team_id, body.name)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"agent name {body.name!r} already exists in this team",
        )

    spec = AgentSpec(
        name=body.name,
        team_id=current.team_id,
        description=body.description,
        created_by=current.user_id,
    )
    session.add(spec)
    await session.flush()

    version = AgentVersion(
        agent_id=spec.id,
        version=1,
        output_schema_key=body.output_schema_key,
        system_prompt=body.system_prompt,
        tools=list(body.tools),
        created_by=current.user_id,
    )
    session.add(version)
    await session.commit()

    return _spec_to_response(spec, latest=1)


@specs_router.get("", response_model=list[AgentSpecResponse])
async def list_specs(
    current: Annotated[UserContext, Depends(get_current_user)],
    session: AsyncSession = Depends(_session_proxy),
) -> list[AgentSpecResponse]:
    """Team-scoped list. Other teams' specs are invisible (not 403)."""
    specs = await list_specs_for_team(session, current.team_id)
    result: list[AgentSpecResponse] = []
    for spec in specs:
        latest = await _resolve_latest_version_number(session, spec.id)
        result.append(_spec_to_response(spec, latest))
    return result


@specs_router.get("/{spec_id}", response_model=AgentSpecResponse)
async def get_spec(
    spec_id: UUID,
    current: Annotated[UserContext, Depends(get_current_user)],
    session: AsyncSession = Depends(_session_proxy),
) -> AgentSpecResponse:
    spec = await get_spec_for_team(session, spec_id, current.team_id)
    if spec is None:
        # 404 not 403: cross-team enumeration defense.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="agent spec not found"
        )
    latest = await _resolve_latest_version_number(session, spec.id)
    return _spec_to_response(spec, latest)


@specs_router.post(
    "/{spec_id}/versions",
    response_model=AgentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    spec_id: UUID,
    body: AgentVersionCreate,
    current: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> AgentVersionResponse:
    """Append a new immutable AgentVersion. version = max(version) + 1."""
    spec = await get_spec_for_team(session, spec_id, current.team_id)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="agent spec not found"
        )
    latest = await get_latest_version_number(session, spec.id)
    version = AgentVersion(
        agent_id=spec.id,
        version=latest + 1,
        output_schema_key=body.output_schema_key,
        system_prompt=body.system_prompt,
        tools=list(body.tools),
        created_by=current.user_id,
    )
    session.add(version)
    await session.commit()
    return _version_to_response(version)


@specs_router.get(
    "/{spec_id}/versions", response_model=list[AgentVersionResponse]
)
async def list_spec_versions(
    spec_id: UUID,
    current: Annotated[UserContext, Depends(get_current_user)],
    session: AsyncSession = Depends(_session_proxy),
) -> list[AgentVersionResponse]:
    spec = await get_spec_for_team(session, spec_id, current.team_id)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="agent spec not found"
        )
    versions = await list_versions(session, spec.id)
    return [_version_to_response(v) for v in versions]


# Exposed for the run endpoint module to share the same helpers.
__all__ = [
    "_resolve_latest_version_number",
    "_spec_to_response",
    "_version_to_response",
    "get_latest_version",
    "specs_router",
]
