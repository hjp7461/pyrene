"""Unit tests for `_period_label` / `_period_start` (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime

from pyrene_metering.aggregation import _period_label, _period_start


def test_day_label_iso() -> None:
    when = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
    assert _period_label("day", when) == "2026-05-11"


def test_week_label_iso() -> None:
    """2026-05-11 is a Monday — ISO week 20 (Python's %G-W%V)."""
    when = datetime(2026, 5, 11, tzinfo=UTC)
    assert _period_label("week", when) == "2026-W20"


def test_month_label() -> None:
    when = datetime(2026, 5, 11, tzinfo=UTC)
    assert _period_label("month", when) == "2026-05"


def test_day_start_truncates_to_midnight() -> None:
    when = datetime(2026, 5, 11, 14, 30, 59, 123, tzinfo=UTC)
    assert _period_start("day", when) == datetime(2026, 5, 11, tzinfo=UTC)


def test_week_start_is_monday() -> None:
    """A Wednesday's bucket starts on the preceding Monday."""
    when = datetime(2026, 5, 13, tzinfo=UTC)  # Wednesday
    start = _period_start("week", when)
    assert start.weekday() == 0  # Monday
    assert start == datetime(2026, 5, 11, tzinfo=UTC)


def test_month_start_is_first_of_month() -> None:
    when = datetime(2026, 5, 31, 23, 59, tzinfo=UTC)
    assert _period_start("month", when) == datetime(2026, 5, 1, tzinfo=UTC)
