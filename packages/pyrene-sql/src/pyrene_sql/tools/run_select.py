"""Structured SELECT tool: I/O contract + executor wiring.

PRD-001 §4.1. The Pydantic AI `Agent.tool` registration arrives in Day 2;
Phase 1 exposes `run_select_direct(session, input)` for direct invocation
from CLI / tests.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator

from pyrene_core import OrderBySpec, StrictBaseModel
from pyrene_sql.tools.models import LLMToolInput

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# "schema.table" — both segments are simple identifiers.
# Phase 2 RBAC depends on this exact shape (F-02, F-08).
_QUALIFIED_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

# A `where` fragment must not embed multiple statements or comments.
# The fragment is still bound via prepared statements (F-03 first defense),
# but we strip the obvious shapes early.
_FORBIDDEN_WHERE_PATTERNS = (";", "--", "/*", "*/")


class RunSelectInput(LLMToolInput):
    """Input contract. PRD-001 §4.1."""

    table: str
    columns: list[str] | Literal["*"]
    where: str | None = None
    where_params: dict[str, Any] = Field(default_factory=dict)
    order_by: list[OrderBySpec] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("table")
    @classmethod
    def _table_must_be_qualified(cls, v: str) -> str:
        if not _QUALIFIED_NAME.match(v):
            raise ValueError(
                "table must be lowercase 'schema.table' (e.g. 'public.film')"
            )
        return v

    @field_validator("columns")
    @classmethod
    def _columns_non_empty(cls, v: list[str] | Literal["*"]) -> list[str] | Literal["*"]:
        if v == "*":
            return v
        if not v:
            raise ValueError("columns must be non-empty list or '*'")
        return v

    @field_validator("where")
    @classmethod
    def _where_no_dangerous_patterns(cls, v: str | None) -> str | None:
        if v is None:
            return None
        for pattern in _FORBIDDEN_WHERE_PATTERNS:
            if pattern in v:
                raise ValueError(
                    f"where fragment must not contain {pattern!r}; "
                    "use named params for values"
                )
        return v


class RunSelectOutput(StrictBaseModel):
    """Output contract. PRD-001 §4.1."""

    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


async def run_select_direct(
    session: AsyncSession, input: RunSelectInput
) -> RunSelectOutput:
    """Phase 1 direct invocation. Day 2 wraps this in a Pydantic AI tool."""
    from pyrene_sql.tools.execution import execute_run_select

    return await execute_run_select(session, input)
