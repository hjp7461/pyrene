"""Unit tests for the pure cost aggregation module (no Streamlit/httpx).

집계 수학(Decimal 정밀·retry 귀속·일 버킷)을 런타임 의존성 없이 검증.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pyrene_mcp_frontend.cost_aggregation import (
    CostDashboardData,
    UsageRow,
    aggregate,
)


def _row(
    *,
    request_id: str = "r1",
    attempt_idx: int = 0,
    model: str = "claude-sonnet-4-6",
    cost: str = "1",
    created: str = "2026-05-16T10:00:00+00:00",
) -> UsageRow:
    return UsageRow(
        request_id=request_id,
        attempt_idx=attempt_idx,
        model=model,
        cost_usd=Decimal(cost),
        created_at=datetime.fromisoformat(created),
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )


def test_empty_rows_all_zero() -> None:
    d = aggregate(())
    assert d == CostDashboardData(
        total_cost=Decimal("0"),
        request_count=0,
        record_count=0,
        retry_overhead_cost=Decimal("0"),
        retry_overhead_pct=0.0,
        retried_request_count=0,
        by_model=(),
        by_time=(),
    )


def test_single_row_no_retry() -> None:
    d = aggregate((_row(cost="2.5"),))
    assert d.total_cost == Decimal("2.5")
    assert d.request_count == 1
    assert d.record_count == 1
    assert d.retry_overhead_cost == Decimal("0")
    assert d.retry_overhead_pct == 0.0
    assert d.retried_request_count == 0
    assert d.by_model == (("claude-sonnet-4-6", Decimal("2.5")),)
    assert d.by_time == (("2026-05-16", Decimal("2.5")),)


def test_retry_overhead_attribution() -> None:
    rows = (
        _row(request_id="A", attempt_idx=0, cost="1"),
        _row(request_id="A", attempt_idx=1, cost="0.5"),
        _row(request_id="B", attempt_idx=0, cost="2"),
    )
    d = aggregate(rows)
    assert d.total_cost == Decimal("3.5")
    assert d.request_count == 2
    assert d.record_count == 3
    assert d.retry_overhead_cost == Decimal("0.5")
    assert d.retried_request_count == 1
    # 0.5 / 3.5 * 100 ≈ 14.2857
    assert abs(d.retry_overhead_pct - (0.5 / 3.5 * 100.0)) < 1e-9


def test_decimal_precision_no_float_drift() -> None:
    rows = tuple(_row(request_id=f"r{i}", cost="0.00000001") for i in range(3))
    d = aggregate(rows)
    assert d.total_cost == Decimal("0.00000003")
    assert isinstance(d.total_cost, Decimal)


def test_by_model_sorted_cost_desc() -> None:
    rows = (
        _row(model="cheap", cost="1", request_id="r1"),
        _row(model="pricey", cost="5", request_id="r2"),
        _row(model="cheap", cost="1", request_id="r3"),
    )
    d = aggregate(rows)
    assert d.by_model == (
        ("pricey", Decimal("5")),
        ("cheap", Decimal("2")),
    )


def test_by_time_day_bucket_sorted_asc() -> None:
    rows = (
        _row(request_id="r1", cost="1", created="2026-05-16T23:00:00+00:00"),
        _row(request_id="r2", cost="2", created="2026-05-15T01:00:00+00:00"),
        _row(request_id="r3", cost="3", created="2026-05-15T22:00:00+00:00"),
    )
    d = aggregate(rows)
    assert d.by_time == (
        ("2026-05-15", Decimal("5")),
        ("2026-05-16", Decimal("1")),
    )


def test_zero_total_cost_pct_guard() -> None:
    rows = (_row(cost="0", request_id="r1"), _row(cost="0", request_id="r2"))
    d = aggregate(rows)
    assert d.total_cost == Decimal("0")
    assert d.retry_overhead_pct == 0.0  # no ZeroDivisionError
