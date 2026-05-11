"""SQL execution for the run_select tool.

The validated `RunSelectInput` already guarantees:
  - `table` matches /^[a-z_][a-z0-9_]*\\.[a-z_][a-z0-9_]*$/
  - `columns` is "*" or a non-empty list of identifiers
  - `where` contains no ';' / '--' / block comment markers
  - `limit` is in [1, 1000]

So we interpolate identifiers directly (after a final identifier check on
columns) and bind values via SQLAlchemy named params. Defense-in-depth at the
DB role still applies (F-03): writes are rejected by Postgres regardless.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_sql.tools.run_select import RunSelectInput, RunSelectOutput

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _validate_column_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"invalid column identifier: {name!r}")
    return name


def _render_columns(columns: list[str] | str) -> str:
    if columns == "*":
        return "*"
    assert isinstance(columns, list)
    return ", ".join(_validate_column_ident(c) for c in columns)


def _render_order_by(specs: list[Any]) -> str:
    if not specs:
        return ""
    parts = [f"{_validate_column_ident(s.column)} {s.direction.upper()}" for s in specs]
    return " ORDER BY " + ", ".join(parts)


def _row_to_jsonable(row: Row[Any]) -> dict[str, Any]:
    return {k: _to_jsonable(v) for k, v in row._mapping.items()}


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        # Preserve precision; downstream serializer chooses str vs float.
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return str(value)


async def execute_run_select(
    session: AsyncSession, input: RunSelectInput
) -> RunSelectOutput:
    """Execute a validated `RunSelectInput` against the bound (read-only) session."""
    cols = _render_columns(input.columns)
    where_clause = f" WHERE {input.where}" if input.where else ""
    order_clause = _render_order_by(input.order_by)
    # Fetch limit + 1 so we can detect truncation without an extra COUNT(*).
    fetch_limit = input.limit + 1

    sql = (
        f"SELECT {cols} FROM {input.table}{where_clause}{order_clause} "
        f"LIMIT :__pyrene_limit"
    )

    params: dict[str, Any] = {**input.where_params, "__pyrene_limit": fetch_limit}
    result = await session.execute(text(sql), params)
    rows = result.fetchall()

    truncated = len(rows) > input.limit
    if truncated:
        rows = rows[: input.limit]

    jsonable = [_row_to_jsonable(r) for r in rows]
    return RunSelectOutput(rows=jsonable, row_count=len(jsonable), truncated=truncated)
