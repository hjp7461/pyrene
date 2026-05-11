"""Budget-specific exception types.

PLAN-014 Day 1. The hook raises these; the FastAPI handler in
`pyrene_budget.routes.handlers` maps them to HTTP responses.

### Why distinct from `PermissionDeniedError`

`PermissionDeniedError` (in pyrene-core) classifies as
`NonRetryableError` → maps to a 403 in the agent retry wrapper. Budget
denial is conceptually closer to 429 (you may retry later — at midnight
when the period resets) but PRD-014 §2.1 S2 specifies *block* (the
retry would just hit the same gate). We expose a typed subclass so the
route layer can map it deterministically.

### `BudgetLockUnavailableError`

Raised on `pg_try_advisory_xact_lock` returning false. Translates to
HTTP 503 (fail-closed) — the conflicting writer holds the lock; the
caller is asked to retry, but importantly we do not silently let the
request through (that's the race we're closing).

### `BudgetSystemUnavailableError`

Catches startup-time misconfigurations (PG_DSN wrong, table missing).
Maps to 503 with a different `detail` string than lock contention so
operators can distinguish the two in logs.

### Naming convention

All exceptions end in `Error` (ruff N818 / repo convention; mirrors
`PermissionDeniedError`, `EmptyResultError` etc. in pyrene-core).
"""

from __future__ import annotations

from decimal import Decimal

from pyrene_core.errors import NonRetryableError


class BudgetError(NonRetryableError):
    """Base for budget-flow exceptions.

    Inherits from `NonRetryableError` so the agent retry wrapper
    (PLAN-003) classifies budget denials as terminal — retrying a
    budget-blocked request without waiting for the period reset would
    just re-hit the gate.
    """


class BudgetExceededError(BudgetError):
    """Pre-flight projection (used + predicted) >= limit.

    Carries the (used, limit, predicted) tuple so the route layer can
    surface a useful diagnostic.
    """

    def __init__(
        self,
        *,
        used_usd: Decimal,
        limit_usd: Decimal,
        predicted_usd: Decimal,
        message: str | None = None,
    ) -> None:
        self.used_usd = used_usd
        self.limit_usd = limit_usd
        self.predicted_usd = predicted_usd
        super().__init__(
            message
            or (
                f"budget exceeded: used={used_usd} predicted={predicted_usd} "
                f">= limit={limit_usd}"
            )
        )


class BudgetLockUnavailableError(BudgetError):
    """`pg_try_advisory_xact_lock` returned false — concurrent pre-flight in flight.

    The hook does not retry: PLAN-014 §위험 신호 #1 specifies that
    the race resolves by serialization, and the contending request will
    progress within milliseconds. The client is told to retry. We
    fail-closed (503), not fail-open.
    """


class BudgetSystemUnavailableError(BudgetError):
    """Generic unrecoverable: DB unreachable, table missing, etc.

    Distinguished from `BudgetLockUnavailableError` because operator
    response differs (here you check DB health; there you wait).
    """


__all__ = [
    "BudgetError",
    "BudgetExceededError",
    "BudgetLockUnavailableError",
    "BudgetSystemUnavailableError",
]
