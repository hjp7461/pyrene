"""Page: Live SQL Analyst — 자연어 → SQL → retry-segment progressive disclosure.

PRD-046 §4.2. The 5th page in pyrene-mcp-frontend. UX 5각형 (PRD-020 /
023 / 026 / 031 / 032 — `code-style.md` Do #8) is observable in source:

  - PRD-020 friendly_error for AgentRunError fallout
  - PRD-023 a spinner wraps the agent run only (not the render path)
  - PRD-026 retry button with `mcp_agent_retry_*` key + cache clear + rerun
  - PRD-031 page-header `🔄 새로 고침` (page-scope, clears cache + rerun)
  - PRD-032 / ADR-018 `fetch_or_stale(...)` wraps the external call so a
    failed re-run still shows the last good response with a 노란 경고 banner

ADR-019 / F-15: this module imports nothing from `pyrene_*` outside
`pyrene_mcp_frontend.*`. The agent response shape is mirrored locally as
`AnalystRunResult` in `agent_client`.
"""

from __future__ import annotations

import streamlit as st

from pyrene_mcp_frontend.agent_client import (
    AgentRunError,
    AnalystRunResult,
    run_agent_with_trace,
)
from pyrene_mcp_frontend.api_client import (
    fetch_or_stale,
    friendly_error,
    get_base_url,
)
from pyrene_mcp_frontend.auth import require_mcp_user
from pyrene_mcp_frontend.progressive_disclosure import (
    render_attempts_progressively,
)
from pyrene_mcp_frontend.retry_logic import should_offer_retry

token = require_mcp_user()


def _render_response_footer(resp: AnalystRunResult) -> None:
    """Per-response footer: cost · audit · Logfire trace link (F-12 signal).

    Each field is independently rendered — a missing one (None) is silently
    skipped so the row never shows half-empty cells.
    """
    cols = st.columns([1, 1, 2])
    if resp.cost_usd is not None:
        cols[0].caption(f"Cost: ${resp.cost_usd}")
    if resp.audit_id:
        cols[1].caption(f"Audit: {str(resp.audit_id)[:8]}…")
    if resp.logfire_trace_url:
        cols[2].link_button("Logfire trace ↗", resp.logfire_trace_url)


# ─── page header (PRD-031 page-level refresh) ─────────────────────────
header_left, header_right = st.columns([0.85, 0.15])
with header_left:
    st.title("SQL Analyst")
with header_right:
    if st.button("🔄 새로 고침", key="mcp_agent_refresh"):
        st.cache_data.clear()
        st.session_state.pop("agent_messages", None)
        st.rerun()

st.caption(
    "자연어로 질문하면 SQL 분석가가 SELECT 쿼리를 작성하고 결과·재시도·비용을 보여줍니다."
)

# ─── chat history ─────────────────────────────────────────────────────
if "agent_messages" not in st.session_state:
    st.session_state["agent_messages"] = []

for msg in st.session_state["agent_messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        resp_in_msg = msg.get("response")
        if isinstance(resp_in_msg, AnalystRunResult):
            _render_response_footer(resp_in_msg)


# ─── input + run ──────────────────────────────────────────────────────
question = st.chat_input("질문을 입력하세요…")
if question:
    q: str = question  # narrow + bind for closure capture
    st.session_state["agent_messages"].append(
        {"role": "user", "content": q, "response": None}
    )
    with st.chat_message("user"):
        st.markdown(q)

    with st.chat_message("assistant"):
        placeholder = st.empty()

        def _fetch() -> AnalystRunResult:
            return run_agent_with_trace(
                question=q,
                jwt=token,
                api_base=get_base_url(),
            )

        run_key = f"agent_run_{len(st.session_state['agent_messages'])}"
        resp: AnalystRunResult | None = None
        exc: Exception | None = None
        try:
            with st.spinner("Agent 가 SQL 을 작성 중…", show_time=False):
                resp = fetch_or_stale(
                    key=run_key,
                    context="SQL Analyst 실행",
                    fetcher=_fetch,
                )
        except AgentRunError as e:
            exc = e
            st.error(friendly_error(e, context="SQL Analyst 실행"))

        if resp is not None:
            render_attempts_progressively(
                resp.attempts, placeholder, delay_s=0.5
            )
            if resp.refusal:
                st.warning(resp.refusal)
            elif resp.rows is not None:
                st.dataframe(list(resp.rows))
                if resp.analysis:
                    st.caption(resp.analysis)
            elif resp.sql:
                st.code(resp.sql, language="sql")

            st.metric("Confidence", resp.confidence or "—")
            _render_response_footer(resp)

        st.session_state["agent_messages"].append(
            {
                "role": "assistant",
                "content": (resp.sql if resp and resp.sql else "(에러)"),
                "response": resp,
            }
        )

        retry_idx = len(st.session_state["agent_messages"])
        if should_offer_retry(resp=resp, exc=exc) and st.button(
            "🔄 재시도", key=f"mcp_agent_retry_{retry_idx}"
        ):
            st.cache_data.clear()
            st.rerun()
