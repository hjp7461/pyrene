"""`before_run` hook factory for data-level RBAC.

PLAN-011 Day 2. The hook closes over:

  - a `DataPermissionResolver`    — cached decision oracle
  - a `session_factory: () -> AsyncIterator[AsyncSession]`
                                  — opens a fresh session per check
                                    so the hook is not tied to the
                                    FastAPI request session lifecycle
  - a `role_lookup: RoleLookup`   — bridges `UserContext.roles` names
                                    to role UUIDs (mirrors `pyrene-rbac`
                                    hook injection)

### Why `RunContext.metadata["tool_input"]`?

The data-RBAC hook needs the **table(s)** the agent is about to read.
For PRD-001 (`run_select`) the table is in `input.table`; for PRD-004
(`run_join`) it is `input.left`, `input.right`, and `input.join.table`;
for `run_aggregate` it is `input.base_table` plus every `joins[i].table`.

PRD-011 §4 specifies `parse_qualified(input.table) → (schema, table)`
just before the executor runs. PLAN-009 RunContext exposes the tool
name but not the tool input — host apps stamp the input into
`RunContext.metadata["tool_input"]` at construction so the hook can
read it without depending on the SQL package directly.

If no tool input is present and `tool_name is None`, the hook returns
without action (the agent-level run path; data-RBAC fires per
tool-call). If `tool_name` is set but no input is in metadata, the
hook denies fast (fail-closed — a configured tool call without an
input payload is a bug or a bypass attempt).

### Schema-qualified bypass surface (PM amend cases 1-5)

The hook normalizes every table reference before consulting the
resolver:

  1. `"public".payment`     → `public.payment` (quote on schema only)
  2. `"public"."payment"`   → `public.payment` (quotes on both)
  3. `PUBLIC.payment`       → `public.payment` (case)
  4. `public . payment`     → `public.payment` (whitespace)
  5. `public.PAYMENT`       → `public.payment` (table case)

Beyond those five, the hook also rejects multi-statement payloads
(SQL injection via `; UNION ...`) by failing fast on any unparseable
reference — `parse_qualified` returns `None`, the hook raises.

### Fail-closed (PRD-011 §F1)

The hook raises `PermissionDeniedError` on every deny (default-deny
included). The gateway re-raises out of `Gateway.run(...)`; the
FastAPI handler maps it to HTTP 403 with the user-language message.

### Registration

```python
resolver = DataPermissionResolver()
hook = make_data_rbac_hook(
    resolver, session_factory=..., role_lookup=...
)
gateway.before_run(hook, priority=PRIORITY_DATA_RBAC)
```

`pyrene_data_rbac.startup.register_hooks` wraps this with
`PRIORITY_DATA_RBAC=30`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core.errors import PermissionDeniedError
from pyrene_data_rbac.permission_resolver import (
    DEFAULT_CONNECTION_ID,
    DataPermissionResolver,
)
from pyrene_gateway import BeforeRunHook, RunContext

# Match the `RoleLookup` / `SessionFactory` shapes from `pyrene-rbac` so
# the host app can pass the same callbacks to both hook factories.
RoleLookup = Callable[[AsyncSession, tuple[str, ...]], Awaitable[tuple[UUID, ...]]]
SessionFactory = Callable[[], AsyncIterator[AsyncSession]]

# Identifier regex that accepts UNQUOTED `schema.table`. Quoted /
# uppercased / whitespace-padded variants are normalized BEFORE the
# regex check by `parse_qualified`.
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def parse_qualified(raw: str) -> tuple[str, str] | None:
    """Normalize a `schema.table` reference into canonical lowercase parts.

    Returns `None` if the reference cannot be parsed — the hook treats
    `None` as a deny signal (fail-closed; PM amend bypass cases 1-5
    must all collapse onto the same canonical form, and anything that
    does NOT must be rejected).

    Normalization rules:
      - strip outer whitespace
      - split on the FIRST '.' that lies outside a double-quoted region
      - on each side, strip whitespace + surrounding double quotes,
        then lowercase
      - the surviving identifiers must match `_IDENT` (unquoted
        identifier syntax) — anything else is rejected because the
        downstream SQL builder accepts only that shape (PRD-001 §4.1).

    The function is intentionally **not** a SQL parser. It is a
    canonicalizer that accepts the five legitimate bypass shapes and
    rejects everything else. Real SQL parsing (e.g. `pglast`) is
    deferred to a follow-up if the surface widens.
    """
    if not raw:
        return None

    # Walk the string once, splitting on the first '.' outside a
    # double-quoted region. This lets `"sch.with.dot".table` (a
    # legitimate but exotic shape) stay as one schema segment if a
    # caller ever uses it; the regex below still rejects identifiers
    # containing dots, so the row would deny anyway. We keep the
    # walker generic to surface ambiguous inputs.
    in_quote = False
    split_idx = -1
    for i, ch in enumerate(raw):
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch == "." and not in_quote:
            split_idx = i
            break
    if split_idx < 0:
        return None
    left = raw[:split_idx]
    right = raw[split_idx + 1 :]

    schema = left.strip().strip('"').lower()
    table = right.strip().strip('"').lower()
    if not _IDENT.match(schema) or not _IDENT.match(table):
        return None
    return schema, table


def _extract_table_references(tool_input: Any) -> tuple[str, ...]:
    """Pull every table reference out of a structured tool input.

    Supports the PRD-001 / PRD-004 input models:
      - `run_select`     → `input.table`
      - `run_join`       → `input.left`, `input.right`, `input.join.table`
      - `run_aggregate`  → `input.base_table`, every `joins[i].table`

    Falls back to dict access so callers stamping the metadata as a
    plain dict (no pydantic instance) still work — the hook should be
    decoupled from the specific tool module.

    Returns a deduplicated tuple of `schema.table` strings in
    declaration order. The hook normalizes + checks each entry; if
    ANY entry is denied the hook raises.
    """
    refs: list[str] = []

    def _add(value: Any) -> None:
        if isinstance(value, str) and value not in refs:
            refs.append(value)

    def _get(obj: Any, name: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    # run_select shape
    _add(_get(tool_input, "table"))
    # run_join shape — left/right table sides + the JoinSpec.table
    _add(_get(tool_input, "left"))
    _add(_get(tool_input, "right"))
    join = _get(tool_input, "join")
    _add(_get(join, "table"))
    # run_aggregate shape — base_table + every joins[i].table
    _add(_get(tool_input, "base_table"))
    joins = _get(tool_input, "joins")
    if joins is not None:
        for j in joins:
            _add(_get(j, "table"))

    return tuple(refs)


def _format_deny_message(
    table_ref: str, role_names: tuple[str, ...]
) -> str:
    """User-language denial message — PROJECT_BRIEF §6.1-7 + PRD-011 §4."""
    if not role_names:
        roles_phrase = "역할이 없는 사용자"
    elif len(role_names) == 1:
        roles_phrase = f"역할 '{role_names[0]}'"
    else:
        joined = ", ".join(f"'{r}'" for r in sorted(role_names))
        roles_phrase = f"역할 ({joined})"
    return (
        f"{roles_phrase}은(는) 테이블 '{table_ref}'에 대한 읽기 권한이 없습니다. "
        "관리자에게 요청하세요."
    )


def make_data_rbac_hook(
    resolver: DataPermissionResolver,
    *,
    session_factory: SessionFactory,
    role_lookup: RoleLookup,
    default_connection_id: UUID = DEFAULT_CONNECTION_ID,
) -> BeforeRunHook:
    """Construct a `before_run` hook bound to `resolver` + DI seams.

    Returned callable has the `BeforeRunHook` Protocol shape:
    `async def(ctx: RunContext) -> None`.

    `default_connection_id` is the fallback when
    `RunContext.metadata["connection_id"]` is unset — Phase 2
    single-connection deployments use the sentinel.
    """

    @asynccontextmanager
    async def _session_cm() -> AsyncIterator[AsyncSession]:
        iterator = session_factory()
        async for session in iterator:
            yield session
            return

    async def _hook(ctx: RunContext) -> None:
        # Policy: no tool_name → skip. The agent-level run path falls
        # through to the agent's own tool calls, each of which fires
        # this hook in turn.
        if ctx.tool_name is None:
            return

        tool_input = ctx.metadata.get("tool_input")
        if tool_input is None:
            # Tool call without a stamped input is either a bug or a
            # bypass attempt. Fail-closed.
            raise PermissionDeniedError(
                f"data-RBAC: '{ctx.tool_name}' invoked without a "
                "structured input — refusing fail-closed (PRD-011 §F1)"
            )

        references = _extract_table_references(tool_input)
        if not references:
            # Some tools (Phase 3 introspection) carry no table
            # reference. Phase 2's read tools always reference at
            # least one — fail-closed when the hook cannot find any.
            raise PermissionDeniedError(
                f"data-RBAC: '{ctx.tool_name}' input has no table "
                "reference — refusing fail-closed (PRD-011 §F1)"
            )

        connection_id = ctx.metadata.get(
            "connection_id", default_connection_id
        )

        user = ctx.user_context
        async with _session_cm() as session:
            role_ids = await role_lookup(session, user.roles)
            for raw in references:
                parsed = parse_qualified(raw)
                if parsed is None:
                    raise PermissionDeniedError(
                        f"data-RBAC: table reference {raw!r} is not a "
                        "valid 'schema.table' — refusing fail-closed "
                        "(PRD-011 §위험 #1, schema-qualified bypass)"
                    )
                schema, table = parsed
                allowed = await resolver.can_access(
                    session,
                    role_ids=role_ids,
                    connection_id=connection_id,
                    schema=schema,
                    table=table,
                )
                if not allowed:
                    raise PermissionDeniedError(
                        _format_deny_message(
                            f"{schema}.{table}", user.roles
                        )
                    )

    return _hook


__all__ = [
    "RoleLookup",
    "SessionFactory",
    "make_data_rbac_hook",
    "parse_qualified",
]
