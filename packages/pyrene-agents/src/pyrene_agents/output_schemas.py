"""Registry-of-Literal output schemas for AgentVersion.output_schema_key.

PLAN-008 Wave 0' amend (Sr. Python Dev HIGH, BRIEF §6.1-2 reflected):

Rejected pattern: dynamic `importlib.import_module("module.Class")` to
resolve a yaml-declared schema. Three problems:
  1. Arbitrary code-execution surface (yaml can declare any importable name).
  2. `importlib.import_module(...).attribute` returns `Any` → mypy --strict
     surfaces a `Any` leak in every consumer.
  3. Hard to enumerate at type-check time — Literal validation impossible.

Accepted pattern: a `Final[dict[str, type[BaseModel]]]` registry, paired
with a `Literal["..."]` type alias whose members are exactly the dict keys.
The registry is the single source of truth; `OutputSchemaKey` is a typed
view onto the same set of strings. A unit-test invariant
(`set(OUTPUT_SCHEMA_REGISTRY.keys()) == set(get_args(OutputSchemaKey))`)
prevents drift.

Trade-off (intentional): adding a new output type now requires a code
change + PR review. Security and type safety beat availability — Phase 2
needs the strict boundary.

Adding a new schema:
  1. Add an entry to `OUTPUT_SCHEMA_REGISTRY` (str → Pydantic class).
  2. Add the same string literal to `OutputSchemaKey`.
  3. Unit test passes (drift check).
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel

from pyrene_sql.agent import AnalystResponse

# The canonical map. Keys are user-facing names that may appear in yaml /
# AgentVersion rows. Values are the Pydantic classes that Pydantic AI
# uses as the agent's `output_type`.
OUTPUT_SCHEMA_REGISTRY: Final[dict[str, type[BaseModel]]] = {
    "AnalystResponse": AnalystResponse,
}

# Literal mirror. The drift invariant is enforced by
# `tests/unit/test_output_schemas.py::test_registry_and_literal_in_sync`.
OutputSchemaKey = Literal["AnalystResponse"]


def resolve_output_schema(key: str) -> type[BaseModel]:
    """Look up the Pydantic class for `key`.

    Raises `KeyError` with a clear message if `key` is not registered.
    Callers that need a Pydantic ValidationError (yaml load, body parsing)
    should validate through `OutputSchemaKey` (or `AgentVersionCreate`)
    first — this function is the runtime guardrail for code paths that
    bypass the schema layer.
    """
    try:
        return OUTPUT_SCHEMA_REGISTRY[key]
    except KeyError as exc:
        raise KeyError(
            f"unknown output_schema_key {key!r}; "
            f"registered: {sorted(OUTPUT_SCHEMA_REGISTRY)}"
        ) from exc


__all__ = [
    "OUTPUT_SCHEMA_REGISTRY",
    "OutputSchemaKey",
    "resolve_output_schema",
]
