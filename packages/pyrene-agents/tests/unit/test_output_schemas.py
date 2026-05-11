"""Unit tests for `OUTPUT_SCHEMA_REGISTRY` + the Literal mirror.

The drift-invariant test is the load-bearing one: if `OutputSchemaKey`'s
members and the dict keys ever disagree, mypy --strict would let bad keys
through `AgentSpecCreate` while the runtime registry would 422 them.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

from pyrene_agents.output_schemas import (
    OUTPUT_SCHEMA_REGISTRY,
    OutputSchemaKey,
    resolve_output_schema,
)
from pyrene_agents.schemas import AgentSpecCreate


def test_registry_and_literal_in_sync() -> None:
    """Invariant: dict keys ≡ Literal members. Drift-prevention guard."""
    dict_keys = set(OUTPUT_SCHEMA_REGISTRY.keys())
    literal_members = set(get_args(OutputSchemaKey))
    assert dict_keys == literal_members, (
        f"drift: registry={sorted(dict_keys)} literal={sorted(literal_members)}"
    )


def test_registry_values_are_basemodel_subclasses() -> None:
    """Every value must be a Pydantic BaseModel subclass."""
    for key, cls in OUTPUT_SCHEMA_REGISTRY.items():
        assert isinstance(cls, type), f"{key} → {cls!r} is not a type"
        assert issubclass(cls, BaseModel), f"{key} → {cls!r} is not a BaseModel"


def test_resolve_known_key_returns_class() -> None:
    cls = resolve_output_schema("AnalystResponse")
    assert issubclass(cls, BaseModel)
    assert cls.__name__ == "AnalystResponse"


def test_resolve_unknown_key_raises_keyerror_with_message() -> None:
    with pytest.raises(KeyError) as exc_info:
        resolve_output_schema("DefinitelyNotRegistered")
    assert "DefinitelyNotRegistered" in str(exc_info.value)
    assert "AnalystResponse" in str(exc_info.value)


def test_agent_spec_create_rejects_unknown_output_schema_key() -> None:
    """Pydantic Literal validation rejects unregistered keys at body parse."""
    with pytest.raises(ValidationError) as exc_info:
        AgentSpecCreate(
            name="bad",
            description="",
            system_prompt="hi",
            output_schema_key="NotARealSchema",  # type: ignore[arg-type]
            tools=(),
        )
    # Pydantic surfaces the Literal constraint in the error payload.
    assert "output_schema_key" in str(exc_info.value)


def test_agent_spec_create_accepts_registered_key() -> None:
    spec = AgentSpecCreate(
        name="sql-analyst",
        description="phase 1",
        system_prompt="hi",
        output_schema_key="AnalystResponse",
        tools=("run_select",),
    )
    assert spec.output_schema_key == "AnalystResponse"
    assert spec.tools == ("run_select",)
