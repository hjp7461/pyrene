"""Execute a validated `RunJoinInput` against the bound read-only session.

PRD-004 §2.1 S2. The builder produces a single SELECT of the shape:

    SELECT {select_clause}
      FROM {left} {INNER|LEFT|RIGHT} JOIN {join.table}
        ON {on_clause}
     [WHERE ...]
     [ORDER BY ...]
     LIMIT :__pyrene_limit

Design choices:
  * **Text-based SQL** (not SQLAlchemy ORM `select()`/`join()`) — every
    identifier passes through `_BARE_IDENT` / `_TABLE_COLUMN` regex guards
    before interpolation, and every value still binds through named params.
    PRD-001's `execute_run_select` uses the same idiom; the readability gain
    of a uniform builder outweighs SQLAlchemy ORM's auto-correlation features
    we'd otherwise lose by re-validating manually anyway.
  * **RIGHT JOIN stays as-is** rather than swapping to LEFT — the PRD-004 §4
    contract names the table that drives the result set, and silently
    swapping would surprise the LLM's row-ordering expectations. Postgres
    supports RIGHT JOIN natively so the text form is correct as written.
  * Row truncation uses the `limit + 1` trick identical to `run_select`.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core import OrderBySpec
from pyrene_sql.tools.execution import _row_to_jsonable
from pyrene_sql.tools.models import RunJoinInput
from pyrene_sql.tools.run_select import RunSelectOutput

_BARE_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_TABLE_COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


def _check_bare(name: str) -> str:
    if not _BARE_IDENT_RE.match(name):
        raise ValueError(f"invalid identifier: {name!r}")
    return name


def _check_table_column(name: str) -> str:
    if not _TABLE_COLUMN_RE.match(name):
        raise ValueError(f"invalid 'table.column' reference: {name!r}")
    return name


def _table_basename(qualified: str) -> str:
    """`"public.payment"` -> `"payment"` (the prefix used to qualify columns).

    The qualified `schema.table` is already validated by the Pydantic input
    model, so the split is safe; we still re-validate the bare half to keep
    this helper defensively correct when re-used outside the tool.
    """
    schema, table = qualified.split(".", 1)
    _check_bare(schema)
    _check_bare(table)
    return table


def _render_select_clause(
    left_qualified: str,
    right_qualified: str,
    select_left: tuple[str, ...] | None,
    select_right: tuple[str, ...] | None,
) -> str:
    """Render the SELECT projection.

    If both sides are `None` we emit `*` (matches PRD-001's `RunSelectInput`
    behaviour). Otherwise each side's columns get prefixed with that side's
    table name (`left_table.col`) so duplicated column names across the two
    tables collide deterministically in the result mapping.
    """
    if select_left is None and select_right is None:
        return "*"

    parts: list[str] = []
    left_table = _table_basename(left_qualified)
    right_table = _table_basename(right_qualified)

    if select_left is None:
        parts.append(f"{left_table}.*")
    else:
        parts.extend(f"{left_table}.{_check_bare(c)}" for c in select_left)

    if select_right is None:
        parts.append(f"{right_table}.*")
    else:
        parts.extend(f"{right_table}.{_check_bare(c)}" for c in select_right)

    return ", ".join(parts)


def _render_on_clause(on_pairs: tuple[tuple[str, str], ...]) -> str:
    return " AND ".join(
        f"{_check_table_column(lhs)} = {_check_table_column(rhs)}"
        for lhs, rhs in on_pairs
    )


def _render_order_by(specs: tuple[OrderBySpec, ...]) -> str:
    if not specs:
        return ""
    parts: list[str] = []
    for s in specs:
        # Permit either bare or qualified column refs in ORDER BY so the LLM
        # can sort by a column on either side of the join.
        col = s.column
        if not (_BARE_IDENT_RE.match(col) or _TABLE_COLUMN_RE.match(col)):
            raise ValueError(f"invalid order_by column: {col!r}")
        parts.append(f"{col} {s.direction.upper()}")
    return " ORDER BY " + ", ".join(parts)


async def execute_run_join(
    session: AsyncSession, input: RunJoinInput
) -> RunSelectOutput:
    """Execute a validated `RunJoinInput`. Returns PRD-001's `RunSelectOutput`."""
    select_clause = _render_select_clause(
        input.left, input.right, input.select_left, input.select_right
    )
    on_clause = _render_on_clause(input.join.on)
    where_clause = f" WHERE {input.where}" if input.where else ""
    order_clause = _render_order_by(input.order_by)
    fetch_limit = input.limit + 1

    # The join table is the `right` side of the JOIN keyword regardless of
    # `type` — `LEFT JOIN right_table` reads naturally. The JoinSpec also
    # carries its own `table`, which the LLM may legitimately point at a
    # third table (an alias step we deliberately do not support here —
    # PRD-004 §3.2). In Phase 1 we trust `join.table == right` and pass
    # `right` directly so a mismatch surfaces as a real DB error rather than
    # being silently ignored.
    sql = (
        f"SELECT {select_clause} "
        f"FROM {input.left} {input.join.type} JOIN {input.join.table} "
        f"ON {on_clause}"
        f"{where_clause}{order_clause} "
        f"LIMIT :__pyrene_limit"
    )

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


def render_run_join_sql(input: RunJoinInput) -> str:
    """Render the SQL string for a `RunJoinInput` without executing it.

    Used by unit tests + future tracing layers (PLAN-006) to verify the
    builder shape without spinning up a DB. The `__pyrene_limit` placeholder
    stays — binding happens at `execute_run_join` time.
    """
    select_clause = _render_select_clause(
        input.left, input.right, input.select_left, input.select_right
    )
    on_clause = _render_on_clause(input.join.on)
    where_clause = f" WHERE {input.where}" if input.where else ""
    order_clause = _render_order_by(input.order_by)
    return (
        f"SELECT {select_clause} "
        f"FROM {input.left} {input.join.type} JOIN {input.join.table} "
        f"ON {on_clause}"
        f"{where_clause}{order_clause} "
        f"LIMIT :__pyrene_limit"
    )


__all__ = ["execute_run_join", "render_run_join_sql"]
