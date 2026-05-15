"""Page: 비용 대시보드 (records-only).

PRD-060 / ADR-029. `/metering/usage/records` 최근 ≤200건 윈도우를
클라이언트 집계 (`/metering/usage` summary 는 배포 컨테이너 503 —
ADR-029). UX 오각형: fetch_or_stale 단일 fetcher + page refresh.
"""

from __future__ import annotations

import streamlit as st

from pyrene_mcp_frontend.api_client import fetch_or_stale, fetch_usage_records
from pyrene_mcp_frontend.auth import require_mcp_user
from pyrene_mcp_frontend.cost_aggregation import aggregate

token = require_mcp_user()

header_left, header_right = st.columns([0.85, 0.15])
with header_left:
    st.title("비용 대시보드")
with header_right:
    if st.button("🔄 새로 고침", key="mcp_cost_refresh"):
        fetch_usage_records.clear()
        st.rerun()

st.caption(
    "최근 최대 200건 기준 (날짜 범위 필터 없음 — `/metering/usage/records` "
    "제약). 현재 팀의 사용량만 집계됩니다."
)

rows = fetch_or_stale(
    key="cost_usage",
    context="사용량 기록",
    fetcher=fetch_usage_records,
    args=(token,),
)

if rows is None:
    st.stop()

if not rows:
    st.info(
        "아직 기록된 사용량이 없습니다. **SQL Analyst** 또는 **도구 실행** "
        "페이지에서 한 번 호출하면 여기서 비용을 확인할 수 있습니다."
    )
    st.stop()

data = aggregate(rows)

c1, c2, c3 = st.columns(3)
c1.metric("💰 총 비용 (USD)", f"${data.total_cost:.6f}")
c1.caption(f"기록 {data.record_count}건 · 요청 {data.request_count}건")
c2.metric("요청 수", data.request_count)
c3.metric(
    "🔁 retry 오버헤드",
    f"${data.retry_overhead_cost:.6f}",
    f"{data.retry_overhead_pct:.1f}% of 지출",
)
c3.caption(f"재시도 발생 요청 {data.retried_request_count}건")

st.subheader("비용 추이 (일별)")
st.line_chart(
    [{"날짜": d, "비용 (USD)": float(v)} for d, v in data.by_time],
    x="날짜",
    y="비용 (USD)",
    use_container_width=True,
)

st.subheader("모델별 비용")
st.bar_chart(
    [{"모델": m, "비용 (USD)": float(v)} for m, v in data.by_model],
    x="모델",
    y="비용 (USD)",
    use_container_width=True,
)

st.subheader("원시 기록")
st.dataframe(
    [
        {
            "시각": r.created_at.isoformat(),
            "model": r.model,
            "attempt": r.attempt_idx,
            "request_id": r.request_id,
            "in": r.input_tokens,
            "out": r.output_tokens,
            "cost_usd": f"{r.cost_usd:.8f}",
        }
        for r in rows
    ],
    use_container_width=True,
    hide_index=True,
)
