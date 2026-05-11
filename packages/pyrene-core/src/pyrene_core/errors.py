"""Domain exception hierarchy shared across Pyrene packages.

PLAN-003 §17 / ADR-002 D1. The retry wrapper (`pyrene_sql.retry`) classifies a
caught exception via `isinstance(...)` against this hierarchy:

- `RetryableError`     → eligible for self-correction (LLM sees the message
                         and tries again, e.g. column typo).
- `NonRetryableError`  → terminal. The retry decision picks abort_low_confidence
                         (resource / empty signal) or abort_high_confidence_refusal
                         (policy denial — refusal is itself the answer).

Phase 2 packages (RBAC, cost) extend these same bases, so they live in
`pyrene-core` rather than `pyrene-sql`.
"""

from __future__ import annotations


class PyreneError(Exception):
    """Base for every Pyrene-classified error."""

    def __init__(self, message: str, *, sql: str | None = None) -> None:
        super().__init__(message)
        self.sql = sql


class RetryableError(PyreneError):
    """Errors the LLM can plausibly correct with a second attempt."""


class NonRetryableError(PyreneError):
    """Errors that will recur on retry — wrapper must abort immediately."""


class SqlSyntaxError(RetryableError):
    """Bad SQL: syntax error, unknown relation / column, type mismatch."""


class ModelToolValidationError(RetryableError):
    """LLM called a structured tool with arguments that failed validation.

    PRD-019 F-4: Pydantic AI raises `UnexpectedModelBehavior` when
    `agent.tool(retries=0)` is configured (builder.py:118) and tool input
    validation fails. The outer `RetryWrapper` only catches `PyreneError`,
    so without this wrap the failure propagates unhandled and the external
    retry pipeline is short-circuited. Wrapping into a RetryableError lets
    decide() apply the N1-N4 policy uniformly — typically classified as
    `retry` (LLM corrects on next attempt) up to the 3-attempt cap.
    """


class EmptyResultError(NonRetryableError):
    """N1 (PRD-003 §2.2): SELECT returned zero rows — retry will yield the same."""


class QueryTimeoutError(NonRetryableError):
    """N2: server-side statement timeout. Resource pressure, do not retry."""


class PermissionDeniedError(NonRetryableError):
    """N3: read-only role rejected the statement — refusal is the answer."""


__all__ = [
    "EmptyResultError",
    "ModelToolValidationError",
    "NonRetryableError",
    "PermissionDeniedError",
    "PyreneError",
    "QueryTimeoutError",
    "RetryableError",
    "SqlSyntaxError",
]
