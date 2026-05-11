"""Page 5 — Live Traces.

Strategy (PLAN-016 amend):
1. Primary: ``st.components.v1.iframe(logfire_url, height=600)``
2. CSP fallback: latest 5 trace metadata list + "Open in Logfire" button
   + a static last-trace screenshot placeholder.
3. Auto-refresh: ``@st.fragment(run_every=30)`` isolates the trace list
   so only that region reruns every 30 s (full page chrome is stable).

Logfire URL: ``LOGFIRE_URL`` env var (default: https://logfire.pydantic.dev).
"""

from __future__ import annotations

import os
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from pyrene_dashboard import auth
from pyrene_dashboard.api_client import fetch_audit_events, friendly_error

st.title("Live Traces")

token = auth.require_admin()

_LOGFIRE_URL = os.environ.get("LOGFIRE_URL", "https://logfire.pydantic.dev")


# ---------------------------------------------------------------------------
# Primary: Logfire iframe (may be blocked by CSP in some environments)
# ---------------------------------------------------------------------------

st.subheader("Logfire Dashboard")
st.caption(
    "If the embed below is blocked by Content Security Policy, "
    "use the fallback section below to view recent trace metadata."
)

try:
    components.iframe(_LOGFIRE_URL, height=600, scrolling=True)
except Exception as exc:
    st.warning(f"iframe could not be rendered: {exc}")


# ---------------------------------------------------------------------------
# Fallback: recent trace metadata list — refreshed every 30 s
# ---------------------------------------------------------------------------


@st.fragment(run_every=30)
def _render_trace_fallback() -> None:
    """Show the most recent 5 trace-related audit events as a fallback."""
    st.divider()
    st.subheader("Recent Traces (fallback)")

    col_btn, _col_spacer = st.columns([1, 3])
    with col_btn:
        st.link_button("Open in Logfire", _LOGFIRE_URL, use_container_width=True)

    try:
        data = fetch_audit_events(token, size=5)
        items: list[dict[str, Any]] = data.get("items", [])

        if not items:
            st.info("No recent trace data available.")
            return

        st.caption("Showing latest 5 audit events as trace proxy (auto-refreshed every 30 s):")
        for event in items:
            outcome: str = event.get("outcome", "")
            icon = "🔴" if outcome in ("deny", "denied") else "🟢"
            label = (
                f"{icon} [{event.get('created_at', '')[:19]}] "
                f"{event.get('event_type', 'unknown')} "
                f"req={str(event.get('request_id', ''))[:8]}…"
            )
            with st.expander(label, expanded=False):
                st.json(
                    {
                        "id": event.get("id"),
                        "event_type": event.get("event_type"),
                        "outcome": event.get("outcome"),
                        "tool_name": event.get("tool_name"),
                        "user_id": event.get("user_id"),
                        "request_id": event.get("request_id"),
                        "created_at": event.get("created_at"),
                    }
                )

    except Exception as exc:
        st.error(friendly_error(exc, context="트레이스 데이터"))

    # Static screenshot placeholder
    st.caption("Last trace screenshot (static placeholder — replace with actual screenshot path):")
    st.info("[Screenshot placeholder] Set LOGFIRE_SCREENSHOT_PATH env var to display a static PNG.")
    screenshot_path = os.environ.get("LOGFIRE_SCREENSHOT_PATH", "")
    if screenshot_path and os.path.isfile(screenshot_path):
        st.image(screenshot_path, caption="Last Logfire trace", use_container_width=True)


_render_trace_fallback()
