"""Idempotency: `UNIQUE(request_id, attempt_idx)` enforcement.

PRD-013 Day 1 completion criteria:
  - same `(request_id, attempt_idx)` re-INSERT → IntegrityError.
  - retries with distinct `attempt_idx` 0/1/2 → 3 rows.
  - **concurrent INSERTs racing on the same (request_id, attempt_idx) →
    exactly 1 succeeds, the rest raise IntegrityError**.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from pyrene_metering import UsageRecord
from pyrene_metering.repository import insert_usage_record

pytestmark = pytest.mark.integration


async def _seed(session: AsyncSession) -> tuple[UUID, UUID]:
    """Insert a user + team in the savepointed transaction. Returns ids."""
    from sqlalchemy import text

    user_id = uuid4()
    team_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, email, password_hash, is_active) "
            "VALUES (:id, :email, :pw, TRUE)"
        ),
        {"id": user_id, "email": f"u-{user_id}@x.test", "pw": "x"},
    )
    await session.execute(
        text("INSERT INTO teams (id, name) VALUES (:id, :name)"),
        {"id": team_id, "name": f"t-{team_id}"},
    )
    await session.flush()
    return user_id, team_id


async def test_three_retries_three_rows(db_session: AsyncSession) -> None:
    """Same request_id with attempt_idx 0/1/2 → three rows, no conflict."""
    user_id, team_id = await _seed(db_session)
    request_id = uuid4()

    for idx in range(3):
        await insert_usage_record(
            db_session,
            request_id=request_id,
            attempt_idx=idx,
            user_id=user_id,
            team_id=team_id,
            agent_id=None,
            model="anthropic:claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=200,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=Decimal("0.00330000"),
        )
    count = await db_session.execute(
        select(func.count()).select_from(UsageRecord).where(
            UsageRecord.request_id == request_id
        )
    )
    assert count.scalar_one() == 3


async def test_duplicate_attempt_idx_raises(db_session: AsyncSession) -> None:
    """Re-INSERT with the same (request_id, attempt_idx) → IntegrityError."""
    user_id, team_id = await _seed(db_session)
    request_id = uuid4()

    await insert_usage_record(
        db_session,
        request_id=request_id,
        attempt_idx=0,
        user_id=user_id,
        team_id=team_id,
        agent_id=None,
        model="anthropic:claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal("0.00330000"),
    )

    with pytest.raises(IntegrityError):
        await insert_usage_record(
            db_session,
            request_id=request_id,
            attempt_idx=0,
            user_id=user_id,
            team_id=team_id,
            agent_id=None,
            model="anthropic:claude-sonnet-4-6",
            input_tokens=999,
            output_tokens=999,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=Decimal("99.00000000"),
        )


async def test_concurrent_inserts_exactly_one_wins(
    engine: AsyncEngine,
) -> None:
    """Race: 10 concurrent INSERTs on the same (request_id, attempt_idx).

    Exactly 1 succeeds, 9 raise IntegrityError. This is the PRD-013
    Day 1 anchor — the UNIQUE constraint is the idempotency mechanism;
    without it, retry+race could double-bill.

    Implementation note: we use independent connections (one per task)
    because the SAVEPOINT-isolated `db_session` would serialize the
    INSERTs through the outer transaction and the conflict would
    surface only at commit. Independent transactions let Postgres
    arbitrate the conflict at INSERT time, matching production.
    """
    user_id = uuid4()
    team_id = uuid4()
    request_id = uuid4()

    # Seed the user + team in an isolated, committed transaction so the
    # racing connections can see the parents at FK-check time.
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as setup_session:
        from sqlalchemy import text

        await setup_session.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active) "
                "VALUES (:id, :email, :pw, TRUE)"
            ),
            {"id": user_id, "email": f"race-{user_id}@x.test", "pw": "x"},
        )
        await setup_session.execute(
            text("INSERT INTO teams (id, name) VALUES (:id, :name)"),
            {"id": team_id, "name": f"team-race-{team_id}"},
        )
        await setup_session.commit()

    async def attempt() -> bool:
        """Return True if INSERT succeeded, False on IntegrityError."""
        try:
            async with factory() as s:
                await insert_usage_record(
                    s,
                    request_id=request_id,
                    attempt_idx=0,
                    user_id=user_id,
                    team_id=team_id,
                    agent_id=None,
                    model="anthropic:claude-sonnet-4-6",
                    input_tokens=100,
                    output_tokens=200,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=Decimal("0.00330000"),
                )
                await s.commit()
                return True
        except IntegrityError:
            return False

    # Cleanup hook: ensure the seeded rows are removed after the race
    # test, even though the race winner committed (the savepoint fixture
    # only rolls back the outer test transaction, not this race's
    # committed work).
    try:
        results = await asyncio.gather(*[attempt() for _ in range(10)])
        wins = sum(1 for r in results if r)
        losses = sum(1 for r in results if not r)
        assert wins == 1, f"expected exactly 1 winner, got {wins}"
        assert losses == 9
    finally:
        async with factory() as cleanup_session:
            from sqlalchemy import text

            await cleanup_session.execute(
                text("DELETE FROM usage_records WHERE request_id = :rid"),
                {"rid": request_id},
            )
            await cleanup_session.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": user_id}
            )
            await cleanup_session.execute(
                text("DELETE FROM teams WHERE id = :id"), {"id": team_id}
            )
            await cleanup_session.commit()
