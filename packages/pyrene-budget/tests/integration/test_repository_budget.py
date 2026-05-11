"""CRUD + aggregation integration tests against real Postgres.

Verifies:
  - `upsert_budget_limit` inserts on first call, updates on second.
  - `get_budget_limit` round-trips.
  - `list_budget_limits` filters by scope/scope_id.
  - `delete_budget_limit` returns True iff the row existed.
  - `status_for(...)` aggregates real `usage_records` against the limit.
  - `try_lock_for_scope` returns True for free key.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_budget.aggregation import remaining_budget, status_for
from pyrene_budget.errors import BudgetSystemUnavailableError
from pyrene_budget.repository import (
    delete_budget_limit,
    get_budget_limit,
    list_budget_limits,
    try_lock_for_scope,
    upsert_budget_limit,
)

pytestmark = pytest.mark.integration


async def _insert_usage(
    session: AsyncSession,
    *,
    user_id: UUID,
    team_id: UUID,
    cost: Decimal,
) -> None:
    await session.execute(
        text(
            "INSERT INTO usage_records ("
            "id, request_id, attempt_idx, user_id, team_id, "
            "model, input_tokens, output_tokens, cost_usd, created_at"
            ") VALUES (:id, :rid, 0, :uid, :tid, 'test', 0, 0, :cost, NOW())"
        ),
        {
            "id": uuid4(),
            "rid": uuid4(),
            "uid": user_id,
            "tid": team_id,
            "cost": cost,
        },
    )


async def test_upsert_insert_then_update(
    db_session: AsyncSession, seeded_user_team: tuple[UUID, UUID]
) -> None:
    user_id, _team_id = seeded_user_team
    row1 = await upsert_budget_limit(
        db_session,
        scope="user",
        scope_id=user_id,
        period="day",
        limit_usd=Decimal("5.00"),
    )
    assert row1.limit_usd == Decimal("5.00000000")
    row2 = await upsert_budget_limit(
        db_session,
        scope="user",
        scope_id=user_id,
        period="day",
        limit_usd=Decimal("10.00"),
    )
    assert row2.id == row1.id
    assert row2.limit_usd == Decimal("10.00000000")


async def test_get_and_delete_roundtrip(
    db_session: AsyncSession, seeded_user_team: tuple[UUID, UUID]
) -> None:
    user_id, _team_id = seeded_user_team
    await upsert_budget_limit(
        db_session,
        scope="user",
        scope_id=user_id,
        period="month",
        limit_usd=Decimal("50.00"),
    )
    row = await get_budget_limit(
        db_session, scope="user", scope_id=user_id, period="month"
    )
    assert row is not None
    assert row.limit_usd == Decimal("50.00000000")

    deleted = await delete_budget_limit(
        db_session, scope="user", scope_id=user_id, period="month"
    )
    assert deleted is True
    deleted_again = await delete_budget_limit(
        db_session, scope="user", scope_id=user_id, period="month"
    )
    assert deleted_again is False


async def test_list_filters_by_scope(
    db_session: AsyncSession, seeded_user_team: tuple[UUID, UUID]
) -> None:
    user_id, team_id = seeded_user_team
    await upsert_budget_limit(
        db_session,
        scope="user",
        scope_id=user_id,
        period="day",
        limit_usd=Decimal("5.00"),
    )
    await upsert_budget_limit(
        db_session,
        scope="team",
        scope_id=team_id,
        period="day",
        limit_usd=Decimal("100.00"),
    )

    user_rows = await list_budget_limits(db_session, scope="user")
    team_rows = await list_budget_limits(db_session, scope="team")
    assert any(r.scope_id == user_id for r in user_rows)
    assert any(r.scope_id == team_id for r in team_rows)
    # Cross-isolation: a `scope="user"` filter never returns the team row.
    assert all(r.scope == "user" for r in user_rows)


async def test_status_for_aggregates_real_usage(
    db_session: AsyncSession, seeded_user_team: tuple[UUID, UUID]
) -> None:
    user_id, team_id = seeded_user_team
    await upsert_budget_limit(
        db_session,
        scope="user",
        scope_id=user_id,
        period="day",
        limit_usd=Decimal("5.00"),
    )
    await _insert_usage(
        db_session, user_id=user_id, team_id=team_id, cost=Decimal("1.25")
    )
    await db_session.flush()

    status = await status_for(
        db_session, scope="user", scope_id=user_id, period="day"
    )
    assert status is not None
    assert status.limit_usd == Decimal("5.00000000")
    assert status.used_usd == Decimal("1.25000000")
    assert status.remaining_usd == Decimal("3.75000000")
    # 1.25 / 5.00 = 25% (quantized 2dp).
    assert status.used_pct == Decimal("25.00")


async def test_remaining_budget_unset_raises(
    db_session: AsyncSession, seeded_user_team: tuple[UUID, UUID]
) -> None:
    user_id, _team_id = seeded_user_team
    with pytest.raises(BudgetSystemUnavailableError):
        await remaining_budget(
            db_session, scope="user", scope_id=user_id, period="day"
        )


async def test_try_lock_acquires_in_isolation(
    db_session: AsyncSession, seeded_user_team: tuple[UUID, UUID]
) -> None:
    user_id, _team_id = seeded_user_team
    acquired = await try_lock_for_scope(
        db_session, scope="user", scope_id=user_id, period="day"
    )
    assert acquired is True
