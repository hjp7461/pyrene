"""PRD-046 §6.2 — `should_offer_retry` decision rule (6 branches).

Mirrors PRD §6.1 retry matrix:
  - network exception → retry
  - refusal IS the answer (F-04 N3) → no retry
  - empty result (F-04 N1) → no retry
  - success with rows → retry (user may want different query)
  - resp + exc both None → retry (edge case)
  - rows=None (e.g. timeout, F-04 N2) → retry

Uses local `AnalystRunResult` dataclass per ADR-019 / F-15.
"""

from __future__ import annotations

from pyrene_mcp_frontend.agent_client import AgentRunError, AnalystRunResult
from pyrene_mcp_frontend.retry_logic import should_offer_retry


def test_network_exception_offers_retry() -> None:
    exc = AgentRunError("network error", status_code=None)
    assert should_offer_retry(resp=None, exc=exc) is True


def test_refusal_no_retry() -> None:
    """refusal IS the answer (F-04 N3) — retry would be misleading."""
    resp = AnalystRunResult(
        confidence="high",
        refusal="이 데이터에 접근 권한이 없습니다.",
    )
    assert should_offer_retry(resp=resp, exc=None) is False


def test_empty_result_no_retry() -> None:
    """rows=() + row_count=0 → empty (F-04 N1). Same query won't help."""
    resp = AnalystRunResult(
        confidence="medium",
        rows=(),
        row_count=0,
        sql="SELECT 1 WHERE 1=0",
    )
    assert should_offer_retry(resp=resp, exc=None) is False


def test_success_with_rows_allows_retry() -> None:
    """Success → still allow retry (user may want to tweak the query)."""
    resp = AnalystRunResult(
        confidence="high",
        rows=({"x": 1},),
        row_count=1,
        sql="SELECT 1",
    )
    assert should_offer_retry(resp=resp, exc=None) is True


def test_resp_none_offers_retry() -> None:
    """resp None + exc None → retry (edge case, shouldn't happen but safe)."""
    assert should_offer_retry(resp=None, exc=None) is True


def test_rows_none_allows_retry() -> None:
    """rows=None (timeout / unknown — F-04 N2) → retry allowed."""
    resp = AnalystRunResult(
        confidence="low",
        rows=None,
        row_count=None,
    )
    assert should_offer_retry(resp=resp, exc=None) is True
