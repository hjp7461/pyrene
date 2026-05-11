"""Pyrene agent registry (PRD-008).

Phase 2 entry: defines `AgentSpec`, `AgentVersion`, the spec-driven Agent
builder, and the FastAPI router surface for spec CRUD + agent execution.

The package is import-cheap: top-level only re-exports schemas / models /
the registry. The Pydantic AI Agent builder is in `pyrene_agents.builder`
(separate module) so unit tests can construct schemas without pulling the
full pydantic-ai stack.
"""

from pyrene_agents.app import make_app
from pyrene_agents.builder import AgentBuildError, build_agent
from pyrene_agents.exporter import (
    PHASE1_DESCRIPTION,
    PHASE1_SPEC_NAME,
    PHASE1_TOOLS,
    build_phase1_spec,
    export_phase1_yaml,
    load_spec_from_yaml,
)
from pyrene_agents.models import AgentSpec, AgentVersion, Base, metadata
from pyrene_agents.output_schemas import (
    OUTPUT_SCHEMA_REGISTRY,
    OutputSchemaKey,
    resolve_output_schema,
)
from pyrene_agents.repository import (
    get_latest_version,
    get_latest_version_number,
    get_spec_by_id,
    get_spec_by_name,
    get_spec_for_team,
    list_specs_for_team,
    list_versions,
)
from pyrene_agents.routes import run_router, specs_router
from pyrene_agents.schemas import (
    AgentRunRequest,
    AgentSpecCreate,
    AgentSpecResponse,
    AgentVersionCreate,
    AgentVersionResponse,
)
from pyrene_agents.tool_registry import (
    ToolCallable,
    ToolNotRegisteredError,
    ToolRegistry,
    default_tool_registry,
)

__version__ = "0.1.0"

__all__ = [
    "OUTPUT_SCHEMA_REGISTRY",
    "PHASE1_DESCRIPTION",
    "PHASE1_SPEC_NAME",
    "PHASE1_TOOLS",
    "AgentBuildError",
    "AgentRunRequest",
    "AgentSpec",
    "AgentSpecCreate",
    "AgentSpecResponse",
    "AgentVersion",
    "AgentVersionCreate",
    "AgentVersionResponse",
    "Base",
    "OutputSchemaKey",
    "ToolCallable",
    "ToolNotRegisteredError",
    "ToolRegistry",
    "build_agent",
    "build_phase1_spec",
    "default_tool_registry",
    "export_phase1_yaml",
    "get_latest_version",
    "get_latest_version_number",
    "get_spec_by_id",
    "get_spec_by_name",
    "get_spec_for_team",
    "list_specs_for_team",
    "list_versions",
    "load_spec_from_yaml",
    "make_app",
    "metadata",
    "resolve_output_schema",
    "run_router",
    "specs_router",
]
