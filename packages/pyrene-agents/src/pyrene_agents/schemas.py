"""Pydantic request/response schemas for the agent registry API.

`AgentVersionCreate.output_schema_key` is typed as `OutputSchemaKey`
(Literal); Pydantic rejects unknown values with a ValidationError at the
yaml / body parse layer. PRD-008 §F1: "스펙에 없는 도구 참조 → 422".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from pyrene_agents.output_schemas import OutputSchemaKey
from pyrene_core import StrictBaseModel
from pyrene_sql.agent import AnalystResponse


class AgentSpecCreate(StrictBaseModel):
    """Request body for `POST /agents/specs`.

    `tools` is a flat list of names that the builder resolves through
    `ToolRegistry`. Names not registered are rejected at build time.
    """

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    system_prompt: str = Field(min_length=1, max_length=16384)
    output_schema_key: OutputSchemaKey
    tools: tuple[str, ...] = Field(default_factory=tuple)


class AgentVersionCreate(StrictBaseModel):
    """Request body for `POST /agents/specs/{spec_id}/versions`."""

    system_prompt: str = Field(min_length=1, max_length=16384)
    output_schema_key: OutputSchemaKey
    tools: tuple[str, ...] = Field(default_factory=tuple)


class AgentSpecResponse(StrictBaseModel):
    """Read shape for `GET /agents/specs/{id}` and the create endpoints."""

    id: UUID
    name: str
    team_id: UUID
    description: str
    created_by: UUID
    created_at: datetime
    latest_version: int


class AgentVersionResponse(StrictBaseModel):
    """Read shape for `GET /agents/specs/{id}/versions`."""

    id: UUID
    agent_id: UUID
    version: int
    output_schema_key: str
    system_prompt: str
    tools: tuple[str, ...]
    created_by: UUID
    created_at: datetime
    published_at: datetime | None


class AgentRunRequest(StrictBaseModel):
    """Request body for `POST /agents/{spec_id}/run`."""

    question: str = Field(min_length=1, max_length=8192)


class AnalystResponseWithObservability(AnalystResponse):
    """PRD-046 §4.1 — additive 3 필드 wrapper for /run-with-trace endpoint.

    기존 /run endpoint 의 AnalystResponse 와 *additive* 호환. 새 필드는
    None default — 기존 호출자 (CLI / pytest / dashboard) 영향 0.
    """

    audit_id: UUID | None = None
    cost_usd: Decimal | None = None
    logfire_trace_url: str | None = None


__all__ = [
    "AgentRunRequest",
    "AgentSpecCreate",
    "AgentSpecResponse",
    "AgentVersionCreate",
    "AgentVersionResponse",
    "AnalystResponseWithObservability",
]
