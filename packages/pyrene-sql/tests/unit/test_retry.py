"""Unit tests for `pyrene_sql.retry`. PLAN-003 §27-29.

Covers PRD-003 §2.2 N1·N2·N3·N4 (decide policy) and the wrapper's
attempt-tracking behaviour with mock async functions (no agent involved).
"""

from __future__ import annotations

import pytest

from pyrene_core import (
    EmptyResultError,
    PermissionDeniedError,
    PyreneError,
    QueryTimeoutError,
    SqlSyntaxError,
)
from pyrene_sql.retry import (
    AttemptTrace,
    RetryDecision,
    RetryWrapper,
    ToolErrorContext,
    decide,
)

# ---------------------------------------------------------------------------
# decide() — N1·N2·N3·N4 policy. 8 assertions across 5 ctxs.
# (Synchronous; no asyncio marker needed.)
# ---------------------------------------------------------------------------


def test_decide_sql_syntax_first_attempt_retries() -> None:
    ctx = ToolErrorContext(
        attempt=1, error=SqlSyntaxError("relation 'films' does not exist")
    )
    assert decide(ctx) is RetryDecision.retry


def test_decide_sql_syntax_second_attempt_retries() -> None:
    ctx = ToolErrorContext(
        attempt=2, error=SqlSyntaxError("column does not exist")
    )
    assert decide(ctx) is RetryDecision.retry


def test_decide_sql_syntax_third_attempt_aborts_low_confidence() -> None:
    """N4 (PRD-003 §2.2): 3 hardcoded — F-04 / L-02."""
    ctx = ToolErrorContext(attempt=3, error=SqlSyntaxError("still wrong"))
    assert decide(ctx) is RetryDecision.abort_low_confidence


def test_decide_empty_result_aborts_low_confidence_attempt_1() -> None:
    """N1: empty result on first attempt — retry would yield the same."""
    ctx = ToolErrorContext(attempt=1, error=EmptyResultError("0 rows"))
    assert decide(ctx) is RetryDecision.abort_low_confidence


def test_decide_empty_result_aborts_regardless_of_attempt() -> None:
    """N1 is attempt-agnostic."""
    ctx = ToolErrorContext(attempt=2, error=EmptyResultError("0 rows"))
    assert decide(ctx) is RetryDecision.abort_low_confidence


def test_decide_query_timeout_aborts_low_confidence() -> None:
    """N2: DB resource pressure — do not retry."""
    ctx = ToolErrorContext(
        attempt=1, error=QueryTimeoutError("statement timeout 5s")
    )
    assert decide(ctx) is RetryDecision.abort_low_confidence


def test_decide_permission_denied_aborts_high_confidence_refusal() -> None:
    """N3: refusal IS the answer — confidence stays high downstream."""
    ctx = ToolErrorContext(
        attempt=1,
        error=PermissionDeniedError("permission denied for table employees"),
    )
    assert decide(ctx) is RetryDecision.abort_high_confidence_refusal


def test_decide_permission_denied_high_confidence_on_attempt_2() -> None:
    """Permission classification is attempt-independent — never converts."""
    ctx = ToolErrorContext(
        attempt=2, error=PermissionDeniedError("read-only role")
    )
    assert decide(ctx) is RetryDecision.abort_high_confidence_refusal


# ---------------------------------------------------------------------------
# RetryWrapper — happy path, recovery, terminal abort.
# ---------------------------------------------------------------------------


async def test_wrapper_success_first_attempt_records_one_trace() -> None:
    wrapper = RetryWrapper(max_attempts=3)

    async def fn(_attempt: int, _last_error: PyreneError | None) -> str:
        return "ok"

    result = await wrapper.run(fn)
    assert result.value == "ok"
    assert len(result.attempts) == 1
    assert result.attempts[0].error is None
    assert result.final_error is None
    assert result.final_decision is None


async def test_wrapper_recovers_on_second_attempt() -> None:
    """1st: SqlSyntaxError → 2nd: success. Two AttemptTraces."""
    wrapper = RetryWrapper(max_attempts=3)
    calls: list[int] = []

    async def fn(attempt: int, _last_error: PyreneError | None) -> str:
        calls.append(attempt)
        if attempt == 1:
            raise SqlSyntaxError(
                "relation 'films' does not exist", sql="SELECT * FROM films"
            )
        return "fixed"

    result = await wrapper.run(fn)
    assert result.value == "fixed"
    assert calls == [1, 2]
    assert len(result.attempts) == 2
    assert result.attempts[0].error is not None
    assert "films" in result.attempts[0].error
    assert result.attempts[0].sql == "SELECT * FROM films"
    assert result.attempts[1].error is None


async def test_wrapper_exhausts_after_three_sql_syntax_errors() -> None:
    """N4: 3 attempts hardcoded. Last decision is abort_low_confidence."""
    wrapper = RetryWrapper(max_attempts=3)
    calls: list[int] = []

    async def fn(attempt: int, _last_error: PyreneError | None) -> str:
        calls.append(attempt)
        raise SqlSyntaxError(f"bad sql {attempt}", sql=f"SELECT {attempt}")

    result = await wrapper.run(fn)
    assert result.value is None
    assert calls == [1, 2, 3]
    assert len(result.attempts) == 3
    assert result.final_decision is RetryDecision.abort_low_confidence
    assert result.final_error is not None
    assert "bad sql 3" in str(result.final_error)


async def test_wrapper_aborts_immediately_on_empty_result() -> None:
    """N1: 1 attempt, no retry."""
    wrapper = RetryWrapper(max_attempts=3)
    calls: list[int] = []

    async def fn(attempt: int, _last_error: PyreneError | None) -> str:
        calls.append(attempt)
        raise EmptyResultError("0 rows")

    result = await wrapper.run(fn)
    assert result.value is None
    assert calls == [1]
    assert len(result.attempts) == 1
    assert result.final_decision is RetryDecision.abort_low_confidence


async def test_wrapper_aborts_immediately_on_permission_denied() -> None:
    """N3: 1 attempt, high-confidence refusal decision."""
    wrapper = RetryWrapper(max_attempts=3)
    calls: list[int] = []

    async def fn(attempt: int, _last_error: PyreneError | None) -> str:
        calls.append(attempt)
        raise PermissionDeniedError("read-only role")

    result = await wrapper.run(fn)
    assert result.value is None
    assert calls == [1]
    assert len(result.attempts) == 1
    assert result.final_decision is RetryDecision.abort_high_confidence_refusal


async def test_wrapper_aborts_immediately_on_query_timeout() -> None:
    """N2: 1 attempt."""
    wrapper = RetryWrapper(max_attempts=3)
    calls: list[int] = []

    async def fn(attempt: int, _last_error: PyreneError | None) -> str:
        calls.append(attempt)
        raise QueryTimeoutError("statement timeout")

    result = await wrapper.run(fn)
    assert result.value is None
    assert calls == [1]
    assert result.final_decision is RetryDecision.abort_low_confidence


async def test_wrapper_passes_last_error_into_callback() -> None:
    """The fn signature carries `last_error` so callers can adjust prompts."""
    wrapper = RetryWrapper(max_attempts=3)
    seen: list[PyreneError | None] = []

    async def fn(attempt: int, last_error: PyreneError | None) -> str:
        seen.append(last_error)
        if attempt == 1:
            raise SqlSyntaxError("boom")
        return "ok"

    await wrapper.run(fn)
    assert seen[0] is None
    assert isinstance(seen[1], SqlSyntaxError)


async def test_attempt_trace_duration_is_non_negative() -> None:
    """Sanity: perf_counter delta is captured as int milliseconds."""
    wrapper = RetryWrapper(max_attempts=2)

    async def fn(_a: int, _e: PyreneError | None) -> str:
        return "ok"

    result = await wrapper.run(fn)
    assert isinstance(result.attempts[0], AttemptTrace)
    assert result.attempts[0].duration_ms >= 0


async def test_wrapper_rejects_invalid_max_attempts() -> None:
    with pytest.raises(ValueError):
        RetryWrapper(max_attempts=0)
