"""Unit tests for yaml export / load round-trip.

The Phase 1 SYSTEM_PROMPT is multi-line; PyYAML's block-style literal must
preserve it byte-for-byte after a round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyrene_agents.exporter import (
    PHASE1_SPEC_NAME,
    PHASE1_TOOLS,
    build_phase1_spec,
    export_phase1_yaml,
    load_spec_from_yaml,
)
from pyrene_agents.schemas import AgentSpecCreate


def test_build_phase1_spec_returns_canonical_payload() -> None:
    spec = build_phase1_spec()
    assert spec.name == PHASE1_SPEC_NAME
    assert spec.output_schema_key == "AnalystResponse"
    assert spec.tools == PHASE1_TOOLS
    assert "SQL analyst" in spec.system_prompt


def test_export_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "phase1.yaml"
    export_phase1_yaml(path)
    assert path.exists() and path.stat().st_size > 0

    reloaded = load_spec_from_yaml(path)
    canonical = build_phase1_spec()

    # Field-for-field equality across the round-trip.
    assert reloaded.name == canonical.name
    assert reloaded.description == canonical.description
    assert reloaded.system_prompt == canonical.system_prompt
    assert reloaded.output_schema_key == canonical.output_schema_key
    assert reloaded.tools == canonical.tools


def test_load_rejects_unknown_output_schema(tmp_path: Path) -> None:
    """Yaml loader must reject unregistered output_schema_key via Literal."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\n"
        "description: ''\n"
        "system_prompt: hi\n"
        "output_schema_key: NotARealSchema\n"
        "tools: []\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception) as exc_info:  # pydantic ValidationError
        load_spec_from_yaml(bad)
    assert "output_schema_key" in str(exc_info.value)


def test_load_rejects_non_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just a list\n- not a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_spec_from_yaml(bad)
    assert "mapping" in str(exc_info.value)


def test_round_trip_through_model_validate() -> None:
    """`AgentSpecCreate` round-trips through model_dump+model_validate."""
    spec = build_phase1_spec()
    dumped = spec.model_dump()
    reloaded = AgentSpecCreate.model_validate(dumped)
    assert reloaded == spec
