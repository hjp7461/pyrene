"""Execute a validated `RunAggregateInput` against the bound read-only session.

PRD-004 §2.1 S1. Builds:

    SELECT {group_by_cols}, {agg_exprs}
      FROM {base_table} [{INNER|LEFT|RIGHT} JOIN {join.table} ON {on}]
     [WHERE ...]
     GROUP BY {group_by}
     [ORDER BY ...]
     LIMIT :__pyrene_limit

The model contract guarantees `aggregations` is non-empty and that `group_by`
is provided when aggregations are. We still re-validate identifiers at the
builder edge because the executor is the last line of defense between user
input and the SQL string the DB driver receives.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core import OrderBySpec
from pyrene_sql.tools.execution import _row_to_jsonable
from pyrene_sql.tools.models import AggregationSpec, RunAggregateInput
from pyrene_sql.tools.run_select import RunSelectOutput

_BARE_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_TABLE_COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

# Lowercase functions match the `AggregationSpec.function` Literal — Postgres
# is case-insensitive on builtin functions, but we emit upper-case to keep
# the rendered SQL readable in traces/logs.
_FN_RENDER = {
    "count": "COUNT",
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
}


def _check_column_ref(col: str) -> str:
    """Allow either a bare identifier or a 'table.column' qualified ref."""
    if not (_BARE_IDENT_RE.match(col) or _TABLE_COLUMN_RE.match(col)):
        raise ValueError(f"invalid column reference: {col!r}")
    return col


def _check_bare(name: str) -> str:
    if not _BARE_IDENT_RE.match(name):
        raise ValueError(f"invalid identifier: {name!r}")
    return name


def _render_aggregation(agg: AggregationSpec) -> str:
    fn = _FN_RENDER[agg.function]
    if agg.column == "*":
        # Only count(*) makes sense — SUM/AVG/MIN/MAX over `*` are nonsense
        # in Postgres. The Pydantic Literal doesn't constrain this combo, so
        # we enforce here.
        if agg.function != "count":
            raise ValueError(
                f"{agg.function}(*) is not supported; use {agg.function}(column)"
            )
        expr = f"{fn}(*)"
    else:
        expr = f"{fn}({_check_bare(agg.column)})"
    if agg.alias is not None:
        expr = f"{expr} AS {_check_bare(agg.alias)}"
    return expr


def _render_group_by(cols: tuple[str, ...]) -> str:
    return ", ".join(_check_column_ref(c) for c in cols)


def _render_order_by(specs: tuple[OrderBySpec, ...]) -> str:
    if not specs:
        return ""
    parts: list[str] = []
    for s in specs:
        col = s.column
        if not (_BARE_IDENT_RE.match(col) or _TABLE_COLUMN_RE.match(col)):
            raise ValueError(f"invalid order_by column: {col!r}")
        parts.append(f"{col} {s.direction.upper()}")
    return " ORDER BY " + ", ".join(parts)


def _render_join_clause(input: RunAggregateInput) -> str:
    """Render the optional single JOIN. Validated to be 0 or 1 by the model."""
    if not input.joins:
        return ""
    join = input.joins[0]
    on_clause = " AND ".join(
        f"{_check_column_ref(lhs)} = {_check_column_ref(rhs)}"
        for lhs, rhs in join.on
    )
    return f" {join.type} JOIN {join.table} ON {on_clause}"


def render_run_aggregate_sql(input: RunAggregateInput) -> str:
    """Render the SQL string for a `RunAggregateInput` without executing it."""
    group_by_clause = _render_group_by(input.group_by)
    agg_clause = ", ".join(_render_aggregation(a) for a in input.aggregations)
    join_clause = _render_join_clause(input)
    where_clause = f" WHERE {input.where}" if input.where else ""
    order_clause = _render_order_by(input.order_by)

    return (
        f"SELECT {group_by_clause}, {agg_clause} "
        f"FROM {input.base_table}{join_clause}{where_clause} "
        f"GROUP BY {group_by_clause}"
        f"{order_clause} "
        f"LIMIT :__pyrene_limit"
    )


async def execute_run_aggregate(
    session: AsyncSession, input: RunAggregateInput
) -> RunSelectOutput:
    """Execute a validated `RunAggregateInput`. Returns `RunSelectOutput`."""
    sql = render_run_aggregate_sql(input)
    fetch_limit = input.limit + 1
    params: dict[str, Any] = {
        **input.where_params,
        "__pyrene_limit": fetch_limit,
    }
    result = await session.execute(text(sql), params)
    rows = result.fetchall()

    truncated = len(rows) > input.limit
    if truncated:
        rows = rows[: input.limit]

    jsonable = [_row_to_jsonable(r) for r in rows]
    return RunSelectOutput(
        rows=jsonable, row_count=len(jsonable), truncated=truncated
    )


__all__ = ["execute_run_aggregate", "render_run_aggregate_sql"]
