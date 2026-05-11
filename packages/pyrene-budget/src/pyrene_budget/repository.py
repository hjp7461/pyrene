"""Data access for `budget_limits` + advisory-lock helpers.

PLAN-014 Day 1. Two responsibilities:
  1. CRUD over `budget_limits` (lookup by composite, list, upsert).
  2. The advisory-lock helper `try_lock_for_scope` that wraps the
     `pg_try_advisory_xact_lock(hashtextextended(...))` call. This is
     the single chokepoint that closes the PRD-014 §위험 신호 #1 race.

### Lock key derivation

```sql
SELECT pg_try_advisory_xact_lock(
  hashtextextended(:scope || ':' || :scope_id::text || ':' || :period, 0)
)
```

The key is `hashtextextended` of the canonical composite string. The
function is 64-bit (Postgres' two-arg form returns `int8`), which is
exactly what `pg_try_advisory_xact_lock(bigint)` expects. We expose
`lock_key_for(scope, scope_id, period)` as a pure helper so unit tests
can exercise the hash without round-tripping the DB.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_budget.models import BudgetLimit
from pyrene_budget.schemas import BudgetPeriod, BudgetScope


def _composite_key(scope: BudgetScope, scope_id: UUID, period: BudgetPeriod) -> str:
    """Canonical composite string used as the advisory-lock key input.

    Matches the SQL `:scope || ':' || :scope_id::text || ':' || :period`
    expression that the hook executes server-side. Exposed so tests can
    assert the key shape without booting Postgres.
    """
    return f"{scope}:{scope_id}:{period}"


async def try_lock_for_scope(
    session: AsyncSession,
    *,
    scope: BudgetScope,
    scope_id: UUID,
    period: BudgetPeriod,
) -> bool:
    """Attempt to acquire the per-(scope, scope_id, period) advisory lock.

    Uses `pg_try_advisory_xact_lock` (non-blocking; returns immediately).
    Auto-released on TXN commit/rollback (no explicit unlock).

    The SQL is the exact PRD-014 §Day 1 spec:
      `pg_try_advisory_xact_lock(hashtextextended(:k, 0))`
    where `:k = scope || ':' || scope_id::text || ':' || period`.

    Implementation note: SQLAlchemy + asyncpg parse `:name` and the
    Postgres `::cast` operator together; the combination
    `:param::text` is rejected by the asyncpg parser. We do the
    `scope_id::text` cast Python-side (`str(scope_id)`) and bind the
    pre-joined composite key as a single text parameter. The on-disk
    semantics are identical — `hashtextextended` operates on the
    same canonical string.

    Returns:
      True  → lock acquired, caller is the sole pre-flight writer in this
              TXN window. Proceed with the check.
      False → another TXN holds the lock right now. Caller MUST raise
              `BudgetLockUnavailableError` (fail-closed).
    """
    # The composite key matches the SQL spec exactly:
    #   scope || ':' || scope_id::text || ':' || period
    # We build it Python-side to dodge the `:param::cast` parser glitch.
    composite = _composite_key(scope, scope_id, period)
    stmt = text(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"
    )
    result = await session.execute(stmt, {"key": composite})
    acquired: bool = bool(result.scalar_one())
    return acquired


async def get_budget_limit(
    session: AsyncSession,
    *,
    scope: BudgetScope,
    scope_id: UUID,
    period: BudgetPeriod,
) -> BudgetLimit | None:
    """Lookup the budget row for the composite key (or None if unset)."""
    stmt = (
        select(BudgetLimit)
        .where(BudgetLimit.scope == scope)
        .where(BudgetLimit.scope_id == scope_id)
        .where(BudgetLimit.period == period)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_budget_limit(
    session: AsyncSession,
    *,
    scope: BudgetScope,
    scope_id: UUID,
    period: BudgetPeriod,
    limit_usd: Decimal,
) -> BudgetLimit:
    """INSERT ... ON CONFLICT (scope, scope_id, period) DO UPDATE.

    Returns the row in either case. The unique constraint shares its
    composite with the advisory-lock key, so concurrent admin upserts
    serialize through the lock (or, when no lock is taken — admin path
    skips it — through the unique constraint's deferred check).

    Identity-map flush note: SQLAlchemy's identity map returns the
    cached ORM instance on `RETURNING`. After an `ON CONFLICT DO
    UPDATE` the cached row's `limit_usd` is the *old* value; we
    `session.refresh(row)` to re-read the persisted columns.
    """
    stmt = (
        pg_insert(BudgetLimit)
        .values(
            scope=scope,
            scope_id=scope_id,
            period=period,
            limit_usd=limit_usd,
        )
        .on_conflict_do_update(
            index_elements=["scope", "scope_id", "period"],
            set_={"limit_usd": limit_usd},
        )
        .returning(BudgetLimit.id)
    )
    new_id = (await session.execute(stmt)).scalar_one()
    await session.flush()
    row = await session.get(BudgetLimit, new_id)
    if row is None:  # pragma: no cover - defensive
        raise RuntimeError("upsert produced no row")
    await session.refresh(row)
    return row


async def list_budget_limits(
    session: AsyncSession,
    *,
    scope: BudgetScope | None = None,
    scope_id: UUID | None = None,
) -> tuple[BudgetLimit, ...]:
    """List filter-narrowed rows. Both filters optional (admin listing)."""
    stmt = select(BudgetLimit)
    if scope is not None:
        stmt = stmt.where(BudgetLimit.scope == scope)
    if scope_id is not None:
        stmt = stmt.where(BudgetLimit.scope_id == scope_id)
    stmt = stmt.order_by(BudgetLimit.created_at.desc())
    result = await session.execute(stmt)
    return tuple(result.scalars())


async def delete_budget_limit(
    session: AsyncSession,
    *,
    scope: BudgetScope,
    scope_id: UUID,
    period: BudgetPeriod,
) -> bool:
    """Delete the matching row. Returns True iff a row was removed."""
    row = await get_budget_limit(
        session, scope=scope, scope_id=scope_id, period=period
    )
    if row is None:
        return False
    await session.delete(row)
    return True


__all__ = [
    "_composite_key",
    "delete_budget_limit",
    "get_budget_limit",
    "list_budget_limits",
    "try_lock_for_scope",
    "upsert_budget_limit",
]
