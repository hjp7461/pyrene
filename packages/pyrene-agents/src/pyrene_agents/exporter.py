"""YAML export / import for AgentSpec.

PLAN-008 Day 3:
  - `export_phase1_yaml(path)` dumps the canonical Phase 1 sql-analyst spec
    (system_prompt from `pyrene_sql.agent.SYSTEM_PROMPT`, output schema
    `AnalystResponse`, tools `[run_select, run_join, run_aggregate]`) to a
    yaml file that round-trips through `load_spec_from_yaml`.
  - `load_spec_from_yaml(path)` parses + validates via `AgentSpecCreate`.

The yaml schema matches `AgentSpecCreate` field names. `output_schema_key`
is validated as a `OutputSchemaKey` Literal — unknown values raise a
ValidationError at load time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pyrene_agents.schemas import AgentSpecCreate

PHASE1_SPEC_NAME = "sql-analyst"
PHASE1_DESCRIPTION = (
    "Phase 1 SQL analyst: structured run_select / run_join / run_aggregate "
    "against the read-only DB role with external retry wrapper."
)
PHASE1_TOOLS: tuple[str, ...] = ("run_select", "run_join", "run_aggregate")


def build_phase1_spec() -> AgentSpecCreate:
    """Construct the canonical Phase 1 sql-analyst AgentSpecCreate.

    Pulls the static SYSTEM_PROMPT from `pyrene_sql.agent` so the yaml is
    byte-for-byte aligned with the Phase 1 in-code agent.
    """
    from pyrene_sql.agent import SYSTEM_PROMPT

    return AgentSpecCreate(
        name=PHASE1_SPEC_NAME,
        description=PHASE1_DESCRIPTION,
        system_prompt=SYSTEM_PROMPT,
        output_schema_key="AnalystResponse",
        tools=PHASE1_TOOLS,
    )


def export_phase1_yaml(path: Path) -> None:
    """Dump the Phase 1 sql-analyst spec to `path`.

    PyYAML's default flow style breaks the multi-line system_prompt across
    lines; we force block style with `default_flow_style=False` + the `|`
    style for strings containing newlines (PyYAML auto-picks `|`).
    """
    spec = build_phase1_spec()
    payload: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "system_prompt": spec.system_prompt,
        "output_schema_key": spec.output_schema_key,
        "tools": list(spec.tools),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            payload,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=120,
        )


def load_spec_from_yaml(path: Path) -> AgentSpecCreate:
    """Load + validate a yaml file as an `AgentSpecCreate`.

    Validation errors (unknown output_schema_key, missing fields) surface
    as Pydantic `ValidationError` — the CLI / route handler maps them to
    the appropriate HTTP code.
    """
    with path.open("r", encoding="utf-8") as f:
        raw: Any = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(
            f"expected a yaml mapping at top level of {path}, got {type(raw).__name__}"
        )
    return AgentSpecCreate.model_validate(raw)


__all__ = [
    "PHASE1_DESCRIPTION",
    "PHASE1_SPEC_NAME",
    "PHASE1_TOOLS",
    "build_phase1_spec",
    "export_phase1_yaml",
    "load_spec_from_yaml",
]
