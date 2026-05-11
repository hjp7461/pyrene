"""Pyrene Admin Dashboard — st.navigation entry point.

PM Wave 0' amend: ``st.navigation`` (Streamlit 1.36+) replaces the
single-file if-else page routing anti-pattern.  Each page is an
independent module; AppTest can run them in isolation.

Run:
    streamlit run src/pyrene_dashboard/app.py

Environment:
    PYRENE_API_URL   Base URL of the Pyrene API   (default: http://localhost:8000)
    LOGFIRE_URL      Logfire dashboard URL          (default: https://logfire.pydantic.dev)
"""

from __future__ import annotations

import streamlit as st

from pyrene_dashboard import auth

# ---------------------------------------------------------------------------
# Page-level st.set_page_config — must be first Streamlit call
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Pyrene Admin",
    page_icon="🔐",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Global CSS — minimum font-size 14px (PRD-016 Day 3 visual requirement)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Auth gate — must happen before st.navigation so non-admin never sees pages
# ---------------------------------------------------------------------------

if not auth.is_authenticated():
    auth.show_login()
    st.stop()

# Authenticated — render logout button
auth.show_logout_button()

# ---------------------------------------------------------------------------
# st.navigation — PM amend mandates this pattern (Streamlit 1.36+)
# ---------------------------------------------------------------------------

overview_page = st.Page(
    "pages/overview.py",
    title="Overview",
    icon="🏠",
    default=True,
)
usage_page = st.Page(
    "pages/usage.py",
    title="Usage",
    icon="📊",
)
rbac_page = st.Page(
    "pages/rbac_matrix.py",
    title="RBAC Matrix",
    icon="🔒",
)
audit_page = st.Page(
    "pages/audit.py",
    title="Audit Timeline",
    icon="📋",
)
traces_page = st.Page(
    "pages/traces.py",
    title="Live Traces",
    icon="🔍",
)

nav = st.navigation(
    [overview_page, usage_page, rbac_page, audit_page, traces_page],
    position="sidebar",
)
nav.run()


def main() -> None:
    """CLI entry point — delegates to ``streamlit run`` via subprocess."""
    import subprocess
    import sys
    from pathlib import Path

    app_path = Path(__file__).resolve()
    subprocess.run(
        ["streamlit", "run", str(app_path), *sys.argv[1:]],
        check=True,
    )


__all__ = ["main"]
