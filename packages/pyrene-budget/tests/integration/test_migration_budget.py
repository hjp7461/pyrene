"""Migration smoke + round-trip for `0008_budget_limits`.

PLAN-014 Day 1 completion gate: verifies the budget_limits table
exists and the unique composite constraint is enforced.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_budget_limits_table_exists(db_session: AsyncSession) -> None:
    """`budget_limits` is reachable + has the expected columns."""
    res = await db_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'budget_limits' ORDER BY column_name"
        )
    )
    cols = {row[0] for row in res}
    assert {
        "id",
        "scope",
        "scope_id",
        "period",
        "limit_usd",
        "created_at",
        "updated_at",
    } <= cols


async def test_unique_composite_rejects_duplicate(db_session: AsyncSession) -> None:
    """UNIQUE(scope, scope_id, period) — second insert with the same triple fails."""
    sid = uuid4()
    await db_session.execute(
        text(
            "INSERT INTO budget_limits (id, scope, scope_id, period, limit_usd) "
            "VALUES (:id, :s, :sid, :p, :l)"
        ),
        {
            "id": uuid4(),
            "s": "user",
            "sid": sid,
            "p": "day",
            "l": Decimal("1.00"),
        },
    )
    await db_session.flush()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO budget_limits (id, scope, scope_id, period, limit_usd) "
                "VALUES (:id, :s, :sid, :p, :l)"
            ),
            {
                "id": uuid4(),
                "s": "user",
                "sid": sid,
                "p": "day",
                "l": Decimal("2.00"),
            },
        )
        await db_session.flush()
