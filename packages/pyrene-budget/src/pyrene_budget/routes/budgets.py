"""`/budgets` CRUD + `/budgets/{scope}/{scope_id}/status`.

PLAN-014 Day 2. Admin-only CRUD; analyst can read status for the
team it belongs to (route layer enforces).

### Fail-closed exception mapping (PRD-014 L-01)

The pre-flight hook raises into `Gateway.run(...)`. The host app
mounts both the gateway entrypoint route and our exception handlers;
this module exposes `register_exception_handlers(app)` to wire the
mapping in one call:

  - `BudgetLockUnavailableError`   -> HTTP 503 `{detail: "budget service contended"}`
  - `BudgetSystemUnavailableError` -> HTTP 503 `{detail: "budget service unavailable"}`
  - `BudgetExceededError`          -> HTTP 429 `{detail: ..., limit_usd: ...}`

`Retry-After` is omitted from the 429 because period reset is not a
clock-based retry the client should attempt. Operators distinguish
lock vs DB via the `detail` string + log lines.
"""

from __future__ import annotations

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.dependencies import _session_proxy, require_admin, require_any_role
from pyrene_budget.aggregation import status_for
from pyrene_budget.errors import (
    BudgetExceededError,
    BudgetLockUnavailableError,
    BudgetSystemUnavailableError,
)
from pyrene_budget.repository import (
    delete_budget_limit,
    list_budget_limits,
    upsert_budget_limit,
)
from pyrene_budget.schemas import (
    BudgetLimitCreate,
    BudgetLimitResponse,
    BudgetPeriod,
    BudgetScope,
    BudgetStatus,
)
from pyrene_core import UserContext
from pyrene_metering.aggregation import SummaryCache

# Module-level cache holder — mirrors pyrene_metering.routes.usage.
_summary_cache: SummaryCache | None = None


def set_summary_cache(cache: SummaryCache) -> None:
    """Register the host-app `SummaryCache` (call at startup)."""
    global _summary_cache
    _summary_cache = cache


def _require_summary_cache() -> SummaryCache:
    if _summary_cache is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="summary cache not configured",
        )
    return _summary_cache


budgets_router = APIRouter(prefix="/budgets", tags=["budgets"])

# Read endpoint widened to analyst (dashboard); writes admin-only.
_require_reader = require_any_role("admin", "analyst")


def _to_response(row: BudgetLimitResponse) -> BudgetLimitResponse:
    return row


@budgets_router.get("", response_model=list[BudgetLimitResponse])
async def list_budgets(
    _current: Annotated[UserContext, Depends(require_admin)],
    scope: BudgetScope | None = None,
    scope_id: UUID | None = None,
    session: AsyncSession = Depends(_session_proxy),
) -> list[BudgetLimitResponse]:
    """List configured budget limits, optionally narrowed by scope/scope_id."""
    rows = await list_budget_limits(session, scope=scope, scope_id=scope_id)
    return [
        BudgetLimitResponse(
            id=r.id,
            scope=r.scope,  # type: ignore[arg-type]
            scope_id=r.scope_id,
            period=r.period,  # type: ignore[arg-type]
            limit_usd=r.limit_usd,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


@budgets_router.post(
    "",
    response_model=BudgetLimitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_or_update_budget(
    body: BudgetLimitCreate,
    _current: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> BudgetLimitResponse:
    """Upsert a budget limit (admin only).

    Matches `(scope, scope_id, period)` to the unique constraint. If a
    row exists, `limit_usd` is updated; otherwise inserted.
    """
    row = await upsert_budget_limit(
        session,
        scope=body.scope,
        scope_id=body.scope_id,
        period=body.period,
        limit_usd=body.limit_usd,
    )
    await session.commit()
    return BudgetLimitResponse(
        id=row.id,
        scope=row.scope,  # type: ignore[arg-type]
        scope_id=row.scope_id,
        period=row.period,  # type: ignore[arg-type]
        limit_usd=row.limit_usd,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@budgets_router.delete(
    "/{scope}/{scope_id}/{period}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_budget(
    scope: BudgetScope,
    scope_id: UUID,
    period: BudgetPeriod,
    _current: Annotated[UserContext, Depends(require_admin)],
    session: AsyncSession = Depends(_session_proxy),
) -> None:
    deleted = await delete_budget_limit(
        session, scope=scope, scope_id=scope_id, period=period
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="budget not found",
        )
    await session.commit()


@budgets_router.get(
    "/{scope}/{scope_id}/status",
    response_model=BudgetStatus,
)
async def get_budget_status(
    scope: BudgetScope,
    scope_id: UUID,
    _current: Annotated[UserContext, Depends(_require_reader)],
    period: BudgetPeriod = "day",
    session: AsyncSession = Depends(_session_proxy),
) -> BudgetStatus:
    """Current used / limit / remaining + 0..100 pct.

    Used by PLAN-016 dashboards. Returns 404 if no budget configured.
    """
    cache = _require_summary_cache()
    snapshot = await status_for(
        session,
        scope=scope,
        scope_id=scope_id,
        period=period,
        cache=cache,
    )
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no budget configured for the requested scope/period",
        )
    return snapshot


# --- Exception handlers (PRD-014 L-01: fail-closed mapping) ---------------


async def _handle_lock(_req: Request, exc: BudgetLockUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "budget service contended",
            "reason": "advisory_lock_unavailable",
            "message": str(exc),
        },
    )


async def _handle_system(_req: Request, exc: BudgetSystemUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "budget service unavailable",
            "reason": "budget_system_unavailable",
            "message": str(exc),
        },
    )


async def _handle_exceeded(_req: Request, exc: BudgetExceededError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "detail": "budget exceeded",
            "reason": "budget_exceeded",
            "limit_usd": str(exc.limit_usd),
            "used_usd": str(exc.used_usd),
            "predicted_usd": str(exc.predicted_usd),
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Mount handlers for all three budget exception classes.

    Call once at startup. The handlers translate the typed errors
    raised by the pre-flight hook (`Gateway.run(...)` propagates) into
    deterministic HTTP responses.
    """
    # FastAPI's add_exception_handler signature uses a broad
    # `Callable[[Request, Exception], ...]`; cast at the boundary so
    # the precise-subclass handler signatures above stay legible.
    app.add_exception_handler(BudgetLockUnavailableError, cast(Any, _handle_lock))
    app.add_exception_handler(BudgetSystemUnavailableError, cast(Any, _handle_system))
    app.add_exception_handler(BudgetExceededError, cast(Any, _handle_exceeded))


__all__ = [
    "budgets_router",
    "register_exception_handlers",
    "set_summary_cache",
]
