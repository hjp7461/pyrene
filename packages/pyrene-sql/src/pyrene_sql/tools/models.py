"""Input/output models for the structured JOIN + aggregate tools.

PRD-004 §4. Mirrors PRD-001's contract style: frozen `StrictBaseModel`,
tuples (not lists) for sequence fields so the values are immutable, named
`where_params` for prepared-statement binding, and explicit "schema.table"
qualification on every table reference.

The two cross-validations that warrant call-outs:
  - `RunAggregateInput.joins` is capped at length 1 (PRD-004 §3.2 — 3+ table
    JOINs are out of scope for Phase 1).
  - `RunAggregateInput` requires `group_by` to be non-empty whenever
    `aggregations` is non-empty. Pydantic's `model_validator(mode='after')`
    enforces this so the rejection happens at the tool input boundary, not
    at SQL build time (PRD-004 §6 — input validation must catch this).
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from pyrene_core import OrderBySpec, StrictBaseModel


class LLMToolInput(StrictBaseModel):
    """Structured tool-input base that tolerates LLM JSON-stringification.

    `claude-sonnet-4-6` (and peers) serialize non-scalar tool-call
    arguments as JSON strings — e.g. ``columns='["category_id", "name"]'``
    instead of the native array. `StrictBaseModel(extra="forbid")` then
    rejects ``str`` where a list/dict/nested-model is expected, and
    `agent.tool(retries=0)` (ADR-002/ADR-016) gives no in-loop
    self-correction, so every attempt fails identically (PRD-057).

    Decode such strings at the tool boundary: a ``str`` value that JSON-
    decodes to a ``list``/``dict`` is replaced with the decoded value.
    Scalars and native inputs are untouched, and a decode failure keeps
    the original value so the existing ValidationError path still fires
    (no new silent behavior). ADR-027 / F-22. Orthogonal to the retry
    boundary — ``retries=0`` is unchanged.
    """

    @model_validator(mode="before")
    @classmethod
    def _coerce_stringified_json(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        coerced: dict[str, Any] | None = None
        for key, value in data.items():
            if not isinstance(value, str):
                continue
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                continue
            if isinstance(decoded, (list, dict)):
                if coerced is None:
                    coerced = dict(data)
                coerced[key] = decoded
        return coerced if coerced is not None else data

# "schema.table" — both segments are simple lowercase identifiers. Same shape
# as `run_select.py`'s regex so Phase 2 RBAC string match remains uniform.
_QUALIFIED_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

# A bare identifier (column name without table qualifier).
_BARE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

# A "table.column" reference (used on JOIN ON sides + select_left / select_right
# rendering — the table qualifier is what disambiguates same-name columns).
_TABLE_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

# Same fragment guard as PRD-001: a where clause must not embed statement
# terminators or comment openers.
_FORBIDDEN_WHERE_PATTERNS: tuple[str, ...] = (";", "--", "/*", "*/")


def _check_table_qualified(name: str) -> str:
    if not _QUALIFIED_NAME.match(name):
        raise ValueError(
            "table must be lowercase 'schema.table' (e.g. 'public.payment')"
        )
    return name


def _check_where_safe(where: str | None) -> str | None:
    if where is None:
        return None
    for pattern in _FORBIDDEN_WHERE_PATTERNS:
        if pattern in where:
            raise ValueError(
                f"where fragment must not contain {pattern!r}; "
                "use named params for values"
            )
    return where


class JoinSpec(StrictBaseModel):
    """A single JOIN clause. PRD-004 §4.

    `on` is a tuple of `(left_qualified_col, right_qualified_col)` pairs so the
    rendered SQL has the shape `... ON left.id = right.fk AND left.x = right.y`.
    Every column reference must already be qualified with its table name —
    the executor does not invent table aliases, the LLM names them.
    """

    table: str  # "schema.table"
    on: tuple[tuple[str, str], ...]
    type: Literal["INNER", "LEFT", "RIGHT"]

    @field_validator("table")
    @classmethod
    def _table_must_be_qualified(cls, v: str) -> str:
        return _check_table_qualified(v)

    @field_validator("on")
    @classmethod
    def _on_pairs_must_be_qualified(
        cls, v: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        if not v:
            raise ValueError("join.on must contain at least one (left, right) pair")
        for pair in v:
            if len(pair) != 2:
                raise ValueError("each join.on entry must be a (left, right) tuple")
            for side in pair:
                if not _TABLE_COLUMN.match(side):
                    raise ValueError(
                        f"join.on side {side!r} must be 'table.column'"
                    )
        return v


class AggregationSpec(StrictBaseModel):
    """A single aggregation column. PRD-004 §4."""

    function: Literal["count", "sum", "avg", "min", "max"]
    column: str  # bare identifier or "*"
    alias: str | None = None

    @field_validator("column")
    @classmethod
    def _column_is_bare_or_star(cls, v: str) -> str:
        if v == "*":
            return v
        if not _BARE_IDENT.match(v):
            raise ValueError(
                f"aggregation column {v!r} must be a bare identifier or '*'"
            )
        return v

    @field_validator("alias")
    @classmethod
    def _alias_is_bare(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _BARE_IDENT.match(v):
            raise ValueError(f"aggregation alias {v!r} must be a bare identifier")
        return v


class RunJoinInput(LLMToolInput):
    """Input contract for the `run_join` tool. PRD-004 §4.

    `select_left` / `select_right` accept `None` to mean "everything from that
    side" (`*`-equivalent on a per-side basis). Column lists are bare
    identifiers — the executor prefixes them with the side's table at render
    time so the result row keys are stable regardless of qualification.
    """

    left: str
    right: str
    join: JoinSpec
    select_left: tuple[str, ...] | None = None
    select_right: tuple[str, ...] | None = None
    where: str | None = None
    where_params: dict[str, Any] = Field(default_factory=dict)
    order_by: tuple[OrderBySpec, ...] = ()
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("left", "right")
    @classmethod
    def _sides_must_be_qualified(cls, v: str) -> str:
        return _check_table_qualified(v)

    @field_validator("select_left", "select_right")
    @classmethod
    def _select_columns_are_bare(
        cls, v: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if v is None:
            return None
        if not v:
            raise ValueError("select list must be None or non-empty")
        for col in v:
            if not _BARE_IDENT.match(col):
                raise ValueError(f"column {col!r} must be a bare identifier")
        return v

    @field_validator("where")
    @classmethod
    def _where_no_dangerous_patterns(cls, v: str | None) -> str | None:
        return _check_where_safe(v)


class RunAggregateInput(LLMToolInput):
    """Input contract for the `run_aggregate` tool. PRD-004 §4.

    Phase 1 covers base table + optional single JOIN. The model rejects
    `joins` of length 2+ and rejects `aggregations` without a `group_by`
    (cross-field validation in the after-validator). PRD-004 §3.2 + §6.
    """

    base_table: str
    joins: tuple[JoinSpec, ...] = ()
    group_by: tuple[str, ...] = ()
    aggregations: tuple[AggregationSpec, ...]
    where: str | None = None
    where_params: dict[str, Any] = Field(default_factory=dict)
    order_by: tuple[OrderBySpec, ...] = ()
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("base_table")
    @classmethod
    def _base_table_must_be_qualified(cls, v: str) -> str:
        return _check_table_qualified(v)

    @field_validator("joins")
    @classmethod
    def _joins_capped_at_one(cls, v: tuple[JoinSpec, ...]) -> tuple[JoinSpec, ...]:
        # PRD-004 §3.2: 3+ table JOIN deferred. We allow 0 or 1 joins (base
        # table + optional second table).
        if len(v) > 1:
            raise ValueError(
                "run_aggregate supports at most one JOIN (PRD-004 §3.2); "
                f"got {len(v)}"
            )
        return v

    @field_validator("group_by")
    @classmethod
    def _group_by_columns_qualified_or_bare(
        cls, v: tuple[str, ...]
    ) -> tuple[str, ...]:
        # group_by entries may reference either base table columns (bare) or
        # joined table columns (qualified as `table.column`). The executor
        # validates and emits them verbatim.
        for col in v:
            if not _BARE_IDENT.match(col) and not _TABLE_COLUMN.match(col):
                raise ValueError(
                    f"group_by entry {col!r} must be a bare identifier or "
                    "'table.column'"
                )
        return v

    @field_validator("aggregations")
    @classmethod
    def _aggregations_non_empty(
        cls, v: tuple[AggregationSpec, ...]
    ) -> tuple[AggregationSpec, ...]:
        if not v:
            raise ValueError("aggregations must contain at least one entry")
        return v

    @field_validator("where")
    @classmethod
    def _where_no_dangerous_patterns(cls, v: str | None) -> str | None:
        return _check_where_safe(v)

    @model_validator(mode="after")
    def _aggregations_require_group_by(self) -> RunAggregateInput:
        # PRD-004 §6 + §2.2 F2: aggregations without group_by are rejected at
        # the input layer, before any SQL is built. The dual nature of this
        # check ("aggregations non-empty" + "group_by non-empty when aggs
        # exist") makes the failure mode user-facing: the model sees the
        # ValueError text and can self-correct (PRD-003).
        if self.aggregations and not self.group_by:
            raise ValueError(
                "aggregations require at least one group_by column "
                "(PRD-004 §6)"
            )
        return self


__all__ = [
    "AggregationSpec",
    "JoinSpec",
    "RunAggregateInput",
    "RunJoinInput",
]
