"""Pure cost aggregation for the MCP-frontend cost dashboard.

Streamlit/httpx 비의존 — `/metering/usage/records` 행을 받아 대시보드
집계 모델로 변환. F-15: `pyrene_core.StrictBaseModel` 사용 불가
(내부 import 금지) → frozen dataclass (agent_client `AnalystRunResult`
선례). 모든 비용은 `Decimal` 로 누적 (cost_usd 8자리 정밀 보존).

집계 의미론은 PRD-060 / ADR-029 / spec §4.3:
  - retry 오버헤드 = Σ cost where attempt_idx > 0 (재시도 낭비 지출)
  - pct = overhead / total_cost * 100 (total 0 → 0.0 가드)
  - by_time = created_at 의 일(day) 버킷
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class UsageRow:
    """`UsageRecordResponse` 중 집계 필요 필드만 mirror (F-15 로컬 모델)."""

    request_id: str
    attempt_idx: int
    model: str
    cost_usd: Decimal
    created_at: datetime
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


@dataclass(frozen=True)
class CostDashboardData:
    """cost page 가 렌더하는 집계 결과 (불변)."""

    total_cost: Decimal
    request_count: int
    record_count: int
    retry_overhead_cost: Decimal
    retry_overhead_pct: float
    retried_request_count: int
    by_model: tuple[tuple[str, Decimal], ...]
    by_time: tuple[tuple[str, Decimal], ...]


def aggregate(rows: tuple[UsageRow, ...]) -> CostDashboardData:
    """최근 ≤200건 윈도우의 행을 대시보드 집계로 환원 (순수 함수)."""
    total_cost = Decimal("0")
    retry_overhead_cost = Decimal("0")
    by_model_acc: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_time_acc: defaultdict[str, Decimal] = defaultdict(Decimal)
    request_ids: set[str] = set()
    retried_request_ids: set[str] = set()

    for r in rows:
        total_cost += r.cost_usd
        request_ids.add(r.request_id)
        by_model_acc[r.model] += r.cost_usd
        by_time_acc[r.created_at.date().isoformat()] += r.cost_usd
        if r.attempt_idx > 0:
            retry_overhead_cost += r.cost_usd
            retried_request_ids.add(r.request_id)

    retry_overhead_pct = (
        float(retry_overhead_cost / total_cost) * 100.0
        if total_cost > 0
        else 0.0
    )

    by_model = tuple(
        sorted(by_model_acc.items(), key=lambda kv: kv[1], reverse=True)
    )
    by_time = tuple(sorted(by_time_acc.items(), key=lambda kv: kv[0]))

    return CostDashboardData(
        total_cost=total_cost,
        request_count=len(request_ids),
        record_count=len(rows),
        retry_overhead_cost=retry_overhead_cost,
        retry_overhead_pct=retry_overhead_pct,
        retried_request_count=len(retried_request_ids),
        by_model=by_model,
        by_time=by_time,
    )


__all__ = ["CostDashboardData", "UsageRow", "aggregate"]
