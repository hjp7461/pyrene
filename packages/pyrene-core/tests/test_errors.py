"""Smoke tests for the PyreneError hierarchy. PLAN-003 §17."""

from __future__ import annotations

from pyrene_core import (
    EmptyResultError,
    NonRetryableError,
    PermissionDeniedError,
    PyreneError,
    QueryTimeoutError,
    RetryableError,
    SqlSyntaxError,
)


def test_pyrene_error_is_exception_subclass() -> None:
    assert issubclass(PyreneError, Exception)


def test_retryable_vs_nonretryable_split() -> None:
    assert issubclass(RetryableError, PyreneError)
    assert issubclass(NonRetryableError, PyreneError)
    # Domain leaves on the correct side.
    assert issubclass(SqlSyntaxError, RetryableError)
    assert issubclass(EmptyResultError, NonRetryableError)
    assert issubclass(QueryTimeoutError, NonRetryableError)
    assert issubclass(PermissionDeniedError, NonRetryableError)


def test_pyrene_error_carries_optional_sql() -> None:
    e = SqlSyntaxError("bad column", sql="SELECT x FROM y")
    assert e.sql == "SELECT x FROM y"
    assert str(e) == "bad column"


def test_pyrene_error_sql_defaults_to_none() -> None:
    e = EmptyResultError("no rows")
    assert e.sql is None
