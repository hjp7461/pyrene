"""Page: 마지막 invocation 의 Logfire 트레이스 링크.

PRD-040 F-4. invoke 페이지에서 마지막 실행을 session_state 에 저장한 뒤
이 페이지에서 deep link 로 노출 (F-12 시그널).
"""

from __future__ import annotations

import streamlit as st

from pyrene_mcp_frontend.api_client import logfire_trace_url
from pyrene_mcp_frontend.auth import require_mcp_user

require_mcp_user()

header_left, header_right = st.columns([0.85, 0.15])
with header_left:
    st.title("실행 트레이스")
with header_right:
    if st.button("🔄 새로 고침", key="mcp_trace_refresh"):
        st.rerun()

last = st.session_state.get("mcp_last_invoke")
if not last:
    st.info(
        "아직 실행한 도구가 없습니다. **도구 실행** 페이지에서 한 번 호출하면 "
        "여기서 Logfire 트레이스 링크를 확인할 수 있습니다."
    )
    st.stop()

st.caption(
    f"server: `{last['server']}` · tool: `{last['tool']}` · "
    f"latency: {last['latency_ms']:.1f}ms"
)

url = logfire_trace_url(last["trace_id"])
if url:
    st.markdown(
        f"🔍 [Logfire 트레이스 열기]({url})",
    )
    st.code(last["trace_id"], language="text")
else:
    st.warning(
        "트레이스 ID 가 비어 있습니다 — Logfire instrumentation 이 설정되지 "
        "않았거나 (`LOGFIRE_TOKEN` 미지정) 게이트웨이의 OTel 컨텍스트가 "
        "활성화되지 않았습니다."
    )
