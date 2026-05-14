"""Page: 도구 디스커버리 — 선택한 서버의 도구 목록 조회.

PRD-040 F-2. 외부 fetch 는 fetch_or_stale 경유 (UX 5각형).
"""

from __future__ import annotations

import streamlit as st

from pyrene_mcp_frontend.api_client import (
    fetch_or_stale,
    fetch_servers,
    fetch_tools,
)
from pyrene_mcp_frontend.auth import require_mcp_user

token = require_mcp_user()

header_left, header_right = st.columns([0.85, 0.15])
with header_left:
    st.title("도구 디스커버리")
with header_right:
    if st.button("🔄 새로 고침", key="mcp_discovery_refresh"):
        fetch_servers.clear()
        fetch_tools.clear()
        st.rerun()

servers = fetch_or_stale(
    key="mcp_discovery_servers",
    context="MCP 서버 목록",
    fetcher=fetch_servers,
    args=(token,),
)
if servers is None:
    st.stop()
if not servers:
    st.info("등록된 MCP 서버가 없습니다.")
    st.stop()

server_options = {f"{s['name']} ({s['transport']})": s for s in servers}
choice = st.selectbox(
    "서버 선택",
    options=list(server_options.keys()),
    key="mcp_discovery_server_choice",
)
if choice is None:
    st.stop()

server = server_options[choice]
server_id = server["id"]

tools = fetch_or_stale(
    key=f"mcp_discovery_tools_{server_id}",
    context="도구 목록",
    fetcher=fetch_tools,
    args=(token, server_id),
)
if tools is None:
    st.stop()

if not tools:
    st.warning(
        "이 서버에는 디스커버리된 도구가 없습니다. "
        "관리자가 `POST /gateway/servers/{id}/discover` 로 sync 해야 할 수 있습니다."
    )
    st.stop()

st.caption(f"총 {len(tools)}개의 도구")

for tool in tools:
    with st.container(border=True):
        st.markdown(f"**{tool['name']}**")
        st.caption(tool.get("description") or "(설명 없음)")
        with st.expander("입력 스키마 (JSON)"):
            st.json(tool.get("input_schema", {}))
