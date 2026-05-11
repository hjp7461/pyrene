"""Budget threshold webhook with idempotent dedupe.

PLAN-014 Day 2. 80% / 95% / 100% thresholds fire a POST to the
configured webhook URL (env `BUDGET_ALERT_WEBHOOK`). Dedupe keys are
`(scope, scope_id, period, period_label, threshold)` so the same
threshold cannot fire twice within the same bucket — once a day
crosses 80%, the 80% alert fires exactly once until the next day's
bucket.

### Why TTLCache (not DB)

The threshold alarms are operational signals, not business state. A
24h TTL cache in-process is sufficient: process restart resends the
alert (acceptable — operators get a duplicate, not silence). A DB
table is overkill for Phase 2.

### 100% alarm vs hard-block

The pre-flight hook fail-closes at 100% (request rejected). The 100%
webhook fires *additionally* — it's the operator notice "this user is
now hard-blocked until reset". Decoupling alarm from gate keeps the
ops loop unaware of pre-flight internals.

### Failure mode

Webhook POST failures are **silently dropped** (with a warning log).
PRD-014 §위험 신호 #4: webhook failures must not impact user-visible
flow. Observability is via Logfire.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from cachetools import TTLCache

from pyrene_budget.schemas import BudgetPeriod, BudgetScope

logger = logging.getLogger(__name__)


# Standard alert thresholds — order matters (ascending). The notifier
# fires *at most one* alert per call: when crossing 95%, the 95% alert
# fires (the 80% has presumably already fired in an earlier request).
DEFAULT_THRESHOLDS: tuple[Decimal, ...] = (
    Decimal("80"),
    Decimal("95"),
    Decimal("100"),
)


# Pluggable HTTP poster — production wires httpx, tests inject a stub.
WebhookPoster = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _httpx_poster(url: str, payload: dict[str, Any]) -> None:
    """Default async POST using httpx. Imported lazily to avoid the dep
    cost when the host app does not configure a webhook URL.
    """
    import httpx

    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(url, json=payload)


@dataclass(frozen=True)
class _DedupeKey:
    """In-process dedupe key.

    `period_label` is included so crossing a bucket boundary (next day,
    next week, next month) automatically resets the dedupe — the cache
    key changes naturally; no manual flush required.
    """

    scope: BudgetScope
    scope_id: UUID
    period: BudgetPeriod
    period_label: str
    threshold: Decimal


class BudgetAlerter:
    """Async-safe threshold-crossing notifier.

    Held by the host app at startup; the budget post-flight hook calls
    `maybe_fire(...)` after persisting the realized cost.

    Args:
      url: webhook target. If `None`, the alerter is a no-op (useful
           for dev / test runs without a webhook configured).
      poster: pluggable POST function (test seam).
      thresholds: ascending Decimal pct values. Default `[80, 95, 100]`.
      ttl_seconds: dedupe window. Default 86400 (24h).
    """

    def __init__(
        self,
        *,
        url: str | None,
        poster: WebhookPoster = _httpx_poster,
        thresholds: tuple[Decimal, ...] = DEFAULT_THRESHOLDS,
        ttl_seconds: int = 86_400,
        maxsize: int = 4096,
    ) -> None:
        self._url = url
        self._poster = poster
        # Validate ascending order — silently sorting would mask a bug.
        prev = Decimal("-1")
        for t in thresholds:
            if t <= prev:
                raise ValueError(
                    f"thresholds must be strictly ascending; got {thresholds}"
                )
            prev = t
        self._thresholds = thresholds
        self._fired: TTLCache[_DedupeKey, bool] = TTLCache(
            maxsize=maxsize, ttl=ttl_seconds
        )

    @property
    def url(self) -> str | None:
        return self._url

    @property
    def thresholds(self) -> tuple[Decimal, ...]:
        return self._thresholds

    def _highest_crossed(self, used_pct: Decimal) -> Decimal | None:
        """Highest threshold strictly <= used_pct, or None if below the lowest."""
        crossed: Decimal | None = None
        for t in self._thresholds:
            if used_pct >= t:
                crossed = t
            else:
                break
        return crossed

    async def maybe_fire(
        self,
        *,
        scope: BudgetScope,
        scope_id: UUID,
        period: BudgetPeriod,
        period_label: str,
        used_usd: Decimal,
        limit_usd: Decimal,
        used_pct: Decimal,
    ) -> Decimal | None:
        """If `used_pct` crosses a new threshold, POST one notice.

        Returns the threshold value that fired, or None when nothing
        fired (either no crossing or dedupe-suppressed).
        """
        if self._url is None:
            return None

        crossed = self._highest_crossed(used_pct)
        if crossed is None:
            return None

        key = _DedupeKey(
            scope=scope,
            scope_id=scope_id,
            period=period,
            period_label=period_label,
            threshold=crossed,
        )
        if key in self._fired:
            return None
        # Stamp before posting so concurrent callers don't both fire on
        # the same threshold. TTLCache is not thread-safe for compound
        # ops but the asyncio event loop is single-threaded — the
        # `in` check + assignment is atomic within one coroutine slice.
        self._fired[key] = True

        payload: dict[str, Any] = {
            "scope": scope,
            "scope_id": str(scope_id),
            "period": period,
            "period_label": period_label,
            "threshold_pct": str(crossed),
            "used_usd": str(used_usd),
            "limit_usd": str(limit_usd),
            "used_pct": str(used_pct),
        }
        try:
            await self._poster(self._url, payload)
        except Exception as exc:
            # PRD-014 §위험 신호 #4: silent drop, Logfire only.
            logger.warning(
                "budget webhook POST failed url=%s threshold=%s: %s",
                self._url,
                crossed,
                exc,
            )
        return crossed


__all__ = [
    "DEFAULT_THRESHOLDS",
    "BudgetAlerter",
    "WebhookPoster",
]
