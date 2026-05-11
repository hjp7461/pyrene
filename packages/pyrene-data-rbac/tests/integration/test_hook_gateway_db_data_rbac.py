"""Gateway data-RBAC hook end-to-end against real Postgres.

Combines the resolver + the data-RBAC hook + the gateway hook chain.
The agent itself is stubbed (we are testing the policy gate, not the
agent runtime).

PROJECT_BRIEF §3.2 scenario A (data side):
  - analyst on `public.payment` → hook allows
  - viewer on `public.payment`  → hook denies (only `public.film` granted)
  - admin with wildcard         → hook allows everything
  - schema-qualified bypass (`"public"."payment"`, `PUBLIC.PAYMENT`,
    `public . payment`) — all denied for the viewer (canonicalized
    onto the same row that has no grant)
  - F-03 dual defense: the hook denies BEFORE SQL runs; the DB role
    layer (`pyrene_readonly` analogue) is exercised in
    `test_dual_defense_data_rbac.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.models import Role
from pyrene_core import UserContext
from pyrene_core.errors import PermissionDeniedError
from pyrene_data_rbac import (
    DataPermissionResolver,
    make_data_rbac_hook,
)
from pyrene_data_rbac.models import DataPermission
from pyrene_data_rbac.permission_resolver import DEFAULT_CONNECTION_ID
from pyrene_gateway import (
    PRIORITY_DATA_RBAC,
    Gateway,
    RunContext,
)

pytestmark = pytest.mark.integration


async def _seed_matrix(
    db_session: AsyncSession,
) -> tuple[Role, Role, Role]:
    """Seed three roles + the §3.2 data matrix.

    Returns `(viewer, analyst, admin)`:
      - viewer  → only `public.film`
      - analyst → `public.payment` + `public.film`
      - admin   → wildcard `(public, *)` with is_admin_grant-equivalent
    """
    suffix = uuid4().hex[:8]
    viewer = Role(name=f"viewer-{suffix}", description="")
    analyst = Role(name=f"analyst-{suffix}", description="")
    admin = Role(name=f"admin-{suffix}", description="")
    db_session.add_all([viewer, analyst, admin])
    await db_session.flush()

    db_session.add_all(
        [
            DataPermission(
                role_id=viewer.id,
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table_name="film",
                action="allow",
            ),
            DataPermission(
                role_id=analyst.id,
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table_name="payment",
                action="allow",
            ),
            DataPermission(
                role_id=analyst.id,
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table_name="film",
                action="allow",
            ),
            DataPermission(
                role_id=admin.id,
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table_name="*",
                action="allow",
            ),
        ]
    )
    await db_session.flush()
    return viewer, analyst, admin


async def _drive_hook_chain(
    db_session: AsyncSession,
    *,
    resolver: DataPermissionResolver,
    role: Role,
    tool_name: str,
    tool_input: Any,
) -> None:
    """Drive the canonical `before_hooks()` chain with a pinned tool.

    Returns None on allow, raises `PermissionDeniedError` on deny.
    Mirrors how `Gateway.run()` walks `before_hooks()` — except we
    build the `RunContext` ourselves with `tool_name` pinned so the
    data-RBAC hook fires.
    """

    async def _session_factory() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _role_lookup(
        _session: AsyncSession, _names: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        return (role.id,)

    gateway = Gateway()
    hook = make_data_rbac_hook(
        resolver,
        session_factory=_session_factory,
        role_lookup=_role_lookup,
    )
    gateway.before_run(hook, priority=PRIORITY_DATA_RBAC)

    user_ctx = UserContext(
        user_id=uuid4(),
        team_id=uuid4(),
        roles=(role.name,),
    )
    ctx = RunContext(
        user_context=user_ctx,
        request_id=uuid4(),
        tool_name=tool_name,
        question="q",
        metadata={"tool_input": tool_input},
    )
    for before in gateway.before_hooks():
        await before(ctx)


# -------------------- §3.2 Scenario A.1 / A.2 ----------------------------


async def test_analyst_can_read_payment(db_session: AsyncSession) -> None:
    """§3.2 A.1 — analyst with `public.payment` grant passes."""
    _, analyst, _ = await _seed_matrix(db_session)
    resolver = DataPermissionResolver()
    await _drive_hook_chain(
        db_session,
        resolver=resolver,
        role=analyst,
        tool_name="run_select",
        tool_input={"table": "public.payment"},
    )


async def test_viewer_cannot_read_payment(db_session: AsyncSession) -> None:
    """§3.2 A.2 — viewer has no `payment` row → default-deny."""
    viewer, _, _ = await _seed_matrix(db_session)
    resolver = DataPermissionResolver()
    with pytest.raises(PermissionDeniedError) as exc_info:
        await _drive_hook_chain(
            db_session,
            resolver=resolver,
            role=viewer,
            tool_name="run_select",
            tool_input={"table": "public.payment"},
        )
    msg = str(exc_info.value)
    assert "payment" in msg
    assert "관리자" in msg


async def test_admin_wildcard_allows_any_public_table(
    db_session: AsyncSession,
) -> None:
    _, _, admin = await _seed_matrix(db_session)
    resolver = DataPermissionResolver()
    for tbl in ("payment", "film", "rental", "customer"):
        await _drive_hook_chain(
            db_session,
            resolver=resolver,
            role=admin,
            tool_name="run_select",
            tool_input={"table": f"public.{tbl}"},
        )


# -------------------- Schema-qualified bypass surface ---------------------


@pytest.mark.parametrize(
    "raw",
    [
        '"public".payment',  # case 1: quoted schema
        '"public"."payment"',  # case 2: both quoted
        "PUBLIC.PAYMENT",  # case 3: uppercase
        "public . payment",  # case 4: whitespace
        "public.PAYMENT",  # case 5: table uppercase
    ],
)
async def test_viewer_bypass_attempts_all_denied(
    db_session: AsyncSession, raw: str
) -> None:
    """PM amend bypass cases 1-5 — every variant canonicalizes to
    `public.payment`. The viewer never had that row → all denied."""
    viewer, _, _ = await _seed_matrix(db_session)
    resolver = DataPermissionResolver()
    with pytest.raises(PermissionDeniedError):
        await _drive_hook_chain(
            db_session,
            resolver=resolver,
            role=viewer,
            tool_name="run_select",
            tool_input={"table": raw},
        )


async def test_bypass_via_union_injection_denied(
    db_session: AsyncSession,
) -> None:
    """Additional bypass attempt: UNION JOIN injection → parse_qualified
    returns None → hook denies fail-closed."""
    viewer, _, _ = await _seed_matrix(db_session)
    resolver = DataPermissionResolver()
    with pytest.raises(PermissionDeniedError, match=r"valid 'schema\.table'"):
        await _drive_hook_chain(
            db_session,
            resolver=resolver,
            role=viewer,
            tool_name="run_select",
            tool_input={"table": "public.film UNION SELECT * FROM payment"},
        )


# -------------------- Multi-table tools ----------------------------------


async def test_join_denies_when_any_side_missing(
    db_session: AsyncSession,
) -> None:
    """run_join: viewer has `film` but not `customer` → join denied."""
    viewer, _, _ = await _seed_matrix(db_session)
    resolver = DataPermissionResolver()
    with pytest.raises(PermissionDeniedError, match="customer"):
        await _drive_hook_chain(
            db_session,
            resolver=resolver,
            role=viewer,
            tool_name="run_join",
            tool_input={
                "left": "public.film",
                "right": "public.customer",
                "join": {"table": "public.customer"},
            },
        )


async def test_join_allowed_when_both_sides_granted(
    db_session: AsyncSession,
) -> None:
    """analyst has payment + film → run_join across them passes."""
    _, analyst, _ = await _seed_matrix(db_session)
    resolver = DataPermissionResolver()
    await _drive_hook_chain(
        db_session,
        resolver=resolver,
        role=analyst,
        tool_name="run_join",
        tool_input={
            "left": "public.payment",
            "right": "public.film",
            "join": {"table": "public.film"},
        },
    )
