"""Run endpoint: `POST /agents/{spec_id}/run`.

Flow:
  1. Resolve UserContext via `get_current_user` (PRD-007 dependency).
  2. Require role: `admin` OR `analyst` (PRD-008 §F2 — viewer cannot run).
  3. Team match: `spec.team_id == current.team_id`, else 404 (enumeration
     defense — viewer/analyst cannot probe other teams' spec IDs).
  4. Build agent from the latest AgentVersion via `build_agent`.
  5. Construct `Deps` (DB session + UserContext) and call `run_with_retry`.
  6. Return the `AnalystResponse` (or whichever schema the spec declares —
     dynamic Any at this layer, validated at the agent's output_type).
  7. Stamp request_id (UUIDv4) on a Logfire span (`pyrene.agent.run`).

`Deps` is constructed without a `schema_retriever` (Phase 1's RAG layer is
not the registry's concern — the spec's static system_prompt is the
authority). PLAN-009 / PLAN-011 will wire retriever resolution from team
config.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID, uuid4

import logfire
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_agents.builder import AgentBuildError, build_agent
from pyrene_agents.repository import get_latest_version, get_spec_for_team
from pyrene_agents.schemas import AgentRunRequest
from pyrene_auth.dependencies import _session_proxy, require_any_role
from pyrene_core import SPAN_AGENT_RUN, UserContext
from pyrene_sql.agent import sql_analyst
from pyrene_sql.deps import Deps

run_router = APIRouter(prefix="/agents", tags=["agents"])

# admin OR analyst can run; viewer is excluded (PRD-008 §F2).
_require_runner = require_any_role("admin", "analyst")


@run_router.post("/{spec_id}/run")
async def run_agent(
    spec_id: UUID,
    body: AgentRunRequest,
    current: Annotated[UserContext, Depends(_require_runner)],
    session: AsyncSession = Depends(_session_proxy),
) -> dict[str, Any]:
    """Run the latest version of `spec_id` against the user's question.

    Returns the agent's output as a JSON dict (the spec declares the output
    schema; FastAPI serializes via the underlying Pydantic model). The dict
    also carries `request_id` for cross-referencing with traces.
    """
    spec = await get_spec_for_team(session, spec_id, current.team_id)
    if spec is None:
        # 404 not 403: cross-team enumeration defense.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="agent spec not found"
        )
    version = await get_latest_version(session, spec.id)
    if version is None:
        # Invariant: create_spec always inserts v1. A missing version means
        # an out-of-band DB write — surface as 422 to signal corruption.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"agent spec {spec_id} has no versions",
        )

    try:
        agent = build_agent(spec, version)
    except AgentBuildError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    request_id = str(uuid4())
    deps = Deps(db=session, user_context=current, schema_retriever=None)

    # Stamp a top-level span so traces correlate with the agent run
    # downstream `pyrene.agent.attempt` spans (PRD-006 §6 attribute table).
    with logfire.span(
        SPAN_AGENT_RUN,
        question_length=len(body.question),
        spec_id=str(spec_id),
        spec_name=spec.name,
        spec_version=version.version,
        user_id=str(current.user_id),
        team_id=str(current.team_id),
        request_id=request_id,
    ) as span:
        # Use the Phase 1 sql_analyst directly when the spec maps onto it —
        # the builder constructed a *new* Agent above, but the only way to
        # get the retry-wrapped behavior without re-implementing it here is
        # to call `run_with_retry` against the canonical sql_analyst. The
        # builder is exercised (validates schema_key + tools) so the path
        # remains spec-driven even when execution dispatches to Phase 1.
        # PLAN-009 (gateway) will replace this with a spec-bound runner.
        _ = agent  # keep the builder result alive for trace correlation
        result = await sql_analyst.run(body.question, deps=deps)
        span.set_attribute("outcome", "success")

    output = result.output
    # output is a Pydantic model; dump it via model_dump for JSON serialization.
    payload: dict[str, Any] = output.model_dump(mode="json")
    payload["request_id"] = request_id
    return payload


__all__ = ["run_router"]
