"""Data access for usage records.

Keeps SQL boilerplate out of the hook + route layers. Functions are
intentionally small; the aggregation layer (`aggregation.py`) calls
into `select()` directly because GROUP BY / DATE_TRUNC don't reuse well.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_metering.models import UsageRecord


async def insert_usage_record(
    session: AsyncSession,
    *,
    request_id: UUID,
    attempt_idx: int,
    user_id: UUID,
    team_id: UUID,
    agent_id: UUID | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    cost_usd: Decimal,
) -> UsageRecord:
    """Insert one row. Caller flushes/commits.

    No `ON CONFLICT DO NOTHING` here — the hook layer catches
    `IntegrityError` so it can distinguish race-induced duplicates
    (warning, no alarm) from other failures (alarm). See `hooks.py`.
    """
    row = UsageRecord(
        request_id=request_id,
        attempt_idx=attempt_idx,
        user_id=user_id,
        team_id=team_id,
        agent_id=agent_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
    )
    session.add(row)
    await session.flush()
    return row


async def list_records_by_request(
    session: AsyncSession, request_id: UUID
) -> tuple[UsageRecord, ...]:
    result = await session.execute(
        select(UsageRecord)
        .where(UsageRecord.request_id == request_id)
        .order_by(UsageRecord.attempt_idx)
    )
    return tuple(result.scalars())


async def count_records_for_user(session: AsyncSession, user_id: UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(UsageRecord).where(
            UsageRecord.user_id == user_id
        )
    )
    return int(result.scalar_one())


__all__ = [
    "count_records_for_user",
    "insert_usage_record",
    "list_records_by_request",
]
