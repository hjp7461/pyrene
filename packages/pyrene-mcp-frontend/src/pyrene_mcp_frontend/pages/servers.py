"""Page: MCP 서버 목록 + 기본 health.

PRD-040 F-2 (servers list). 외부 fetch 는 fetch_or_stale 경유 (UX 5각형).
"""

from __future__ import annotations

import streamlit as st

from pyrene_mcp_frontend.api_client import fetch_or_stale, fetch_servers
from pyrene_mcp_frontend.auth import require_mcp_user

token = require_mcp_user()

header_left, header_right = st.columns([0.85, 0.15])
with header_left:
    st.title("MCP 서버")
with header_right:
    if st.button("🔄 새로 고침", key="mcp_servers_refresh"):
        fetch_servers.clear()
        st.rerun()

st.caption(
    "현재 팀에 등록된 MCP 서버 목록입니다. 등록·해제는 관리자 CLI 로 수행하세요."
)

servers = fetch_or_stale(
    key="mcp_servers",
    context="MCP 서버 목록",
    fetcher=fetch_servers,
    args=(token,),
)

if servers is None:
    st.stop()

if not servers:
    st.info("등록된 MCP 서버가 없습니다. 관리자에게 서버 등록을 요청하세요.")
    st.stop()

for server in servers:
    with st.container(border=True):
        cols = st.columns([0.4, 0.2, 0.2, 0.2])
        cols[0].markdown(f"**{server['name']}**")
        cols[0].caption(f"id: `{server['id']}`")
        cols[1].markdown(f"transport: `{server['transport']}`")
        cols[2].markdown(
            "✅ enabled" if server.get("enabled") else "⛔ disabled"
        )
        cols[3].caption(f"updated: {server.get('updated_at', '')}")
        if server.get("command"):
            with st.expander("실행 인자"):
                st.code(
                    f"{server['command']} {' '.join(server.get('args') or [])}",
                    language="bash",
                )
