"""PLAN-014 Day 3 fail-closed scenarios.

Verifies that the budget pre-flight + route handlers fail-closed under
two distinct failure modes:

  1. DB connection error during the pre-flight TXN
     → `BudgetSystemUnavailableError` → 503.
  2. `pg_try_advisory_xact_lock` returns false (lock contended)
     → `BudgetLockUnavailableError` → 503 (different `reason`).

PRD-014 L-01: fail-closed by default. Production override (fail-open)
is a future env-var; tests pin the default.

Route-layer mapping is verified by exercising the handler functions
directly with a stub Request — full FastAPI app boot is unnecessary.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from pyrene_budget import (
    BudgetExceededError,
    BudgetLockUnavailableError,
    BudgetSystemUnavailableError,
    make_budget_pre_hook,
)
from pyrene_budget.routes.budgets import (
    _handle_exceeded,
    _handle_lock,
    _handle_system,
)
from pyrene_core import UserContext
from pyrene_gateway import RunContext


def _ctx() -> RunContext:
    return RunContext(
        user_context=UserContext(
            user_id=uuid4(), team_id=uuid4(), roles=("analyst",)
        ),
        request_id=uuid4(),
    )


async def test_db_down_during_preflight_maps_to_system_unavailable() -> None:
    """A `SQLAlchemyError` during pre-flight is rewrapped as
    `BudgetSystemUnavailableError` (fail-closed 503)."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    # Mimic a connection drop: execute raises immediately.
    session.execute.side_effect = OperationalError(
        "SELECT 1", {}, BaseException("conn closed")
    )

    hook = make_budget_pre_hook(
        session_factory=MagicMock(return_value=session),
        summary_cache=MagicMock(),
    )
    with pytest.raises(BudgetSystemUnavailableError):
        await hook(_ctx())


async def test_lock_miss_does_not_demote_to_system_error() -> None:
    """`BudgetLockUnavailableError` propagates as-is (not rewrapped)."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = False
    session.execute.return_value = lock_result

    hook = make_budget_pre_hook(
        session_factory=MagicMock(return_value=session),
        summary_cache=MagicMock(),
    )
    with pytest.raises(BudgetLockUnavailableError):
        await hook(_ctx())


async def test_handler_lock_returns_503_with_reason() -> None:
    response = await _handle_lock(
        MagicMock(),
        BudgetLockUnavailableError("contention on user:abc:day"),
    )
    assert response.status_code == 503
    body = json.loads(bytes(response.body).decode())
    assert body["reason"] == "advisory_lock_unavailable"
    assert body["detail"] == "budget service contended"


async def test_handler_system_returns_503_with_distinct_reason() -> None:
    response = await _handle_system(
        MagicMock(),
        BudgetSystemUnavailableError("DB unreachable"),
    )
    assert response.status_code == 503
    body = json.loads(bytes(response.body).decode())
    assert body["reason"] == "budget_system_unavailable"
    assert body["detail"] == "budget service unavailable"


async def test_handler_exceeded_returns_429_with_quantities() -> None:
    response = await _handle_exceeded(
        MagicMock(),
        BudgetExceededError(
            used_usd=Decimal("0.99"),
            limit_usd=Decimal("1.00"),
            predicted_usd=Decimal("0.05"),
        ),
    )
    assert response.status_code == 429
    body = json.loads(bytes(response.body).decode())
    assert body["reason"] == "budget_exceeded"
    assert body["limit_usd"] == "1.00"
    assert body["used_usd"] == "0.99"
    assert body["predicted_usd"] == "0.05"
