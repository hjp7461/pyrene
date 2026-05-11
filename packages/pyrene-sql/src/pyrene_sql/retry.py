"""External retry wrapper for the SQL analyst agent.

PLAN-003 §18-21 / ADR-002 D1. The wrapper sits OUTSIDE the Pydantic AI agent
and owns the attempt counter (native per-tool `retries=0` keeps the count
single-sourced here). It classifies caught exceptions via `decide()` against
the N1-N4 policy from PRD-003 §2.2.

`run_with_retry` (in `agent.py`) is the public entry point; this module
provides the building blocks (`decide`, `RetryWrapper`, `AttemptTrace`,
`ToolErrorContext`, `RetryDecision`).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import logfire
from pydantic import ConfigDict

from pyrene_core import (
    SPAN_AGENT_ATTEMPT,
    EmptyResultError,
    NonRetryableError,
    PermissionDeniedError,
    PyreneError,
    QueryTimeoutError,
    StrictBaseModel,
)


class RetryDecision(StrEnum):
    """Outcome of `decide()`. PRD-003 §4 + PLAN-003 §18."""

    retry = "retry"
    abort_low_confidence = "abort_low_confidence"
    abort_high_confidence_refusal = "abort_high_confidence_refusal"


class ToolErrorContext(StrictBaseModel):
    """Inputs to the retry decision. PRD-003 §4."""

    # `error` is a `PyreneError` (a plain Exception) — Pydantic's arbitrary-type
    # path covers it. The model is still frozen / extra=forbid via StrictBaseModel.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        arbitrary_types_allowed=True,
    )

    attempt: int  # 1-indexed (PRD-003 §4)
    error: PyreneError
    last_sql: str | None = None


class AttemptTrace(StrictBaseModel):
    """One attempt of the agent. Accumulated into `AnalystResponse.attempts`."""

    sql: str | None = None
    error: str | None = None  # message only — full exception isn't serialisable
    duration_ms: int


def decide(ctx: ToolErrorContext) -> RetryDecision:
    """Apply the N1-N4 retry policy.

    Order matters: PermissionDenied is the only `high_confidence_refusal` case
    (the refusal IS the answer). Empty / Timeout signal low confidence — the
    user gets a "no result + try again differently" hint. Attempt cap is the
    N4 fallback (PRD-003 §2.2, F-04 / L-02 — 3 hardcoded).
    """
    if isinstance(ctx.error, PermissionDeniedError):
        return RetryDecision.abort_high_confidence_refusal
    if isinstance(ctx.error, EmptyResultError | QueryTimeoutError):
        return RetryDecision.abort_low_confidence
    if isinstance(ctx.error, NonRetryableError):
        # Any other non-retryable not covered above defaults to low.
        return RetryDecision.abort_low_confidence
    if ctx.attempt >= 3:
        return RetryDecision.abort_low_confidence
    return RetryDecision.retry


class RetryResult(StrictBaseModel):
    """`RetryWrapper.run` return shape: success value + collected attempts.

    `value` is `None` when the wrapped function exhausted attempts or aborted
    non-retryably; callers inspect `final_decision` to discriminate.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        arbitrary_types_allowed=True,
    )

    value: Any
    attempts: tuple[AttemptTrace, ...]
    final_error: PyreneError | None = None
    final_decision: RetryDecision | None = None


class RetryWrapper:
    """Loop driver: invoke `fn` up to `max_attempts`, classify on PyreneError.

    Generic across return types — `agent.py:run_with_retry` is the typed
    facade for the sql_analyst case. Used in unit tests as well.
    """

    def __init__(self, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts

    async def run(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> RetryResult:
        attempts: list[AttemptTrace] = []
        last_error: PyreneError | None = None
        last_decision: RetryDecision | None = None

        for attempt_idx in range(1, self.max_attempts + 1):
            # Each attempt is its own child span. The wrapper does not own
            # the parent span (`pyrene.agent.run`) — the caller in
            # `agent.run_with_retry` does — so when the wrapper is used
            # standalone (unit tests) the attempt spans simply land at the
            # top level. PRD-006 §6 expects parent_span_id of every attempt
            # to equal the parent agent.run span id when both are present.
            with logfire.span(
                SPAN_AGENT_ATTEMPT,
                attempt_idx=attempt_idx,
                max_attempts=self.max_attempts,
            ) as attempt_span:
                start = time.perf_counter()
                try:
                    value = await fn(attempt_idx, last_error, *args, **kwargs)
                except PyreneError as exc:
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    attempts.append(
                        AttemptTrace(
                            sql=exc.sql, error=str(exc), duration_ms=duration_ms
                        )
                    )
                    last_error = exc
                    last_decision = decide(
                        ToolErrorContext(
                            attempt=attempt_idx, error=exc, last_sql=exc.sql
                        )
                    )
                    attempt_span.set_attribute("error_type", type(exc).__name__)
                    attempt_span.set_attribute("decision", str(last_decision))
                    attempt_span.set_attribute("duration_ms", duration_ms)
                    if last_decision is RetryDecision.retry:
                        continue
                    break
                else:
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    # Best-effort SQL extraction: callers may stamp `.sql` on
                    # the returned value (e.g. AnalystResponse.sql). Absent
                    # attr → None.
                    sql_used = getattr(value, "sql", None)
                    attempts.append(
                        AttemptTrace(
                            sql=sql_used, error=None, duration_ms=duration_ms
                        )
                    )
                    attempt_span.set_attribute("decision", "success")
                    attempt_span.set_attribute("duration_ms", duration_ms)
                    return RetryResult(
                        value=value,
                        attempts=tuple(attempts),
                        final_error=None,
                        final_decision=None,
                    )

        # Loop exited via break (abort) or fell through (cap reached).
        if last_decision is None:
            last_decision = RetryDecision.abort_low_confidence
        return RetryResult(
            value=None,
            attempts=tuple(attempts),
            final_error=last_error,
            final_decision=last_decision,
        )


__all__ = [
    "AttemptTrace",
    "RetryDecision",
    "RetryResult",
    "RetryWrapper",
    "ToolErrorContext",
    "decide",
]
