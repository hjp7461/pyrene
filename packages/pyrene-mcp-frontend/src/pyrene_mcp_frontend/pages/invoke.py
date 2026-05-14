"""Page: 도구 실행 — 서버/도구 선택 + jsonschema 폼 + invoke + 결과.

PRD-040 F-3. UX 5각형 invariant 준수: fetch_or_stale + 🔄 새로 고침.
도구 실행 자체는 *cached 안 함* (audit/budget 부작용).
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from pyrene_mcp_frontend import jsonschema_form
from pyrene_mcp_frontend.api_client import (
    fetch_or_stale,
    fetch_servers,
    fetch_tools,
    friendly_error,
    invoke_tool,
    logfire_trace_url,
)
from pyrene_mcp_frontend.auth import require_mcp_user

token = require_mcp_user()

header_left, header_right = st.columns([0.85, 0.15])
with header_left:
    st.title("도구 실행")
with header_right:
    if st.button("🔄 새로 고침", key="mcp_invoke_refresh"):
        fetch_servers.clear()
        fetch_tools.clear()
        st.rerun()

# ---- 1. server pick ----
servers = fetch_or_stale(
    key="mcp_invoke_servers",
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
    key="mcp_invoke_server_choice",
)
if choice is None:
    st.stop()
server = server_options[choice]
server_id: str = server["id"]

# ---- 2. tool pick ----
tools = fetch_or_stale(
    key=f"mcp_invoke_tools_{server_id}",
    context="도구 목록",
    fetcher=fetch_tools,
    args=(token, server_id),
)
if tools is None:
    st.stop()
if not tools:
    st.warning("이 서버에 디스커버리된 도구가 없습니다.")
    st.stop()

tool_by_name = {t["name"]: t for t in tools}
tool_name = st.selectbox(
    "도구 선택",
    options=list(tool_by_name.keys()),
    key="mcp_invoke_tool_choice",
)
if tool_name is None:
    st.stop()
tool = tool_by_name[tool_name]
st.caption(tool.get("description") or "(설명 없음)")

# ---- 3. argument form (jsonschema_form) ----
st.subheader("인자")
schema = tool.get("input_schema") or {}
try:
    widgets = jsonschema_form.render(schema)
except NotImplementedError as exc:
    st.error(
        f"이 도구의 스키마는 UI 폼으로 표시할 수 없습니다 — {exc} "
        "관리자 CLI 로 직접 호출하세요."
    )
    st.stop()

values: dict[str, Any] = {}
if not widgets:
    st.caption("이 도구는 인자가 없습니다.")
else:
    for w in widgets:
        widget_key = f"mcp_invoke_arg_{tool_name}_{w.field_name}"
        kw: dict[str, Any] = {"key": widget_key, **w.kwargs}
        label = w.label + (" *" if w.required else "")
        if w.kind == "text_input":
            raw = st.text_input(label, **kw)
            values[w.field_name] = raw
        elif w.kind == "number_input":
            values[w.field_name] = st.number_input(label, **kw)
        elif w.kind == "checkbox":
            values[w.field_name] = st.checkbox(label, **kw)
        elif w.kind == "selectbox":
            values[w.field_name] = st.selectbox(label, **kw)
        elif w.kind == "text_area_csv":
            raw_text = st.text_area(label, **kw)
            parser = jsonschema_form.PARSERS["csv_to_str_list"]
            values[w.field_name] = parser(raw_text)

# ---- 4. invoke ----
st.divider()
if st.button("▶️ 실행", key=f"mcp_invoke_run_{tool_name}", type="primary"):
    try:
        with st.spinner("도구 실행 중…", show_time=True):
            response = invoke_tool(token, server_id, tool_name, values)
    except Exception as exc:
        st.error(friendly_error(exc, context=f"{tool_name!r} 실행"))
    else:
        st.success(f"실행 완료 — {response['latency_ms']:.1f}ms")
        st.session_state["mcp_last_invoke"] = {
            "server": server["name"],
            "tool": tool_name,
            "trace_id": response["trace_id"],
            "latency_ms": response["latency_ms"],
        }

        st.subheader("결과")
        result = response["result"]
        if isinstance(result, (dict, list)):
            st.json(result)
        else:
            st.code(json.dumps(result, ensure_ascii=False, indent=2))

        url = logfire_trace_url(response["trace_id"])
        if url:
            st.markdown(f"🔍 [Logfire 트레이스 열기]({url})")
        else:
            st.caption("트레이스 ID 미수집 (Logfire instrumentation 미설정)")
