"""Pyrene MCP Frontend — st.navigation entry point.

Mirrors `pyrene-dashboard.app`. Run:
    streamlit run packages/pyrene-mcp-frontend/src/pyrene_mcp_frontend/app.py

Environment:
    PYRENE_API_URL   Base URL of the Pyrene API   (default: http://localhost:8000)
    LOGFIRE_URL      Logfire dashboard URL          (default: https://logfire.pydantic.dev)
"""

from __future__ import annotations

import streamlit as st

from pyrene_mcp_frontend import auth

st.set_page_config(
    page_title="Pyrene MCP",
    page_icon="🔌",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Global CSS — minimum 14px font (mirrors dashboard, PRD-016 Day 3 visual).
st.markdown(
    """
    <style>
    html, body, [class*="css"] {
        font-size: 14px !important;
    }
    .stDataFrame td, .stDataFrame th {
        font-size: 14px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def main() -> None:
    """Entry point for `pyrene-mcp-frontend` script."""
    if not auth.is_authenticated():
        auth.show_login()
        return

    # Page registry (st.navigation, Streamlit 1.36+).
    nav = st.navigation(
        [
            st.Page(
                "pages/servers.py",
                title="MCP 서버",
                icon="🖥️",
                default=True,
            ),
            st.Page("pages/tool_discovery.py", title="도구 디스커버리", icon="🧭"),
            st.Page("pages/invoke.py", title="도구 실행", icon="▶️"),
            st.Page("pages/trace.py", title="실행 트레이스", icon="🔍"),
            st.Page("pages/agent.py", title="SQL Analyst", icon="🤖"),
            st.Page("pages/cost.py", title="비용 대시보드", icon="💰"),
        ]
    )
    auth.show_logout_button()
    nav.run()


if __name__ == "__main__":
    main()
