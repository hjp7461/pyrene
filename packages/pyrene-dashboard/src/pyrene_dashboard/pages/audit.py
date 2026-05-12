"""Page 4 — Audit Timeline.

Features:
- Filter form (event_type, user_id, since, scope, request_id) inside
  ``st.form`` — "Apply filter" submit prevents live rerun on each widget change.
- Timeline: ``st.line_chart`` — count per hour from /audit/events/timeline.
- Per-row JSON metadata: ``st.expander`` (each row opens independently).
- Auto-refresh: ``@st.fragment(run_every=30)`` wraps the data region.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from pyrene_dashboard import auth
from pyrene_dashboard.api_client import (
    fetch_audit_events,
    fetch_audit_timeline,
    fetch_or_stale,
)

st.title("Audit Timeline")

token = auth.require_admin()

# ---------------------------------------------------------------------------
# Filter form
# ---------------------------------------------------------------------------

with st.form("audit_filter_form"):
    st.subheader("Filters")
    col1, col2 = st.columns(2)

    with col1:
        f_event_type = st.text_input(
            "Event type", key="audit_event_type", placeholder="e.g. rbac_deny"
        )
        f_user_id = st.text_input("User ID", key="audit_user_id")
    with col2:
        f_since = st.date_input("Since (UTC)", value=None, key="audit_since")
        f_scope = st.text_input("Scope", key="audit_scope", placeholder="e.g. team")

    f_request_id = st.text_input("Request ID", key="audit_request_id")

    page_size_opts = [10, 25, 50, 100]
    f_page_size = st.selectbox("Rows per page", page_size_opts, index=1, key="audit_size")

    audit_submitted = st.form_submit_button("Apply filter")

audit_page_num = st.number_input("Page", min_value=1, value=1, step=1, key="audit_page_num")


# ---------------------------------------------------------------------------
# Data region: auto-refresh every 30 s
# ---------------------------------------------------------------------------


@st.fragment(run_every=30)
def _render_audit() -> None:
    """Render timeline chart + paginated event table."""
    # ---- Timeline chart ----
    st.subheader("Event Timeline (hourly counts)")

    since_str: str | None = None
    if f_since:
        import pendulum

        since_str = pendulum.datetime(
            f_since.year, f_since.month, f_since.day, tz="UTC"
        ).to_iso8601_string()

    timeline_data = fetch_or_stale(
        key="audit_timeline",
        context="감사 타임라인",
        fetcher=fetch_audit_timeline,
        args=(token,),
        kwargs={"since": since_str},
    )
    if timeline_data:
        timeline_df = pd.DataFrame(timeline_data)
        timeline_df["bucket"] = pd.to_datetime(timeline_df["bucket"], utc=True)
        timeline_df = timeline_df.set_index("bucket").sort_index()
        st.line_chart(timeline_df["count"], use_container_width=True)
    elif timeline_data is not None:
        st.info("No timeline data available for the selected range.")

    st.divider()

    # ---- Paginated event table ----
    st.subheader("Audit Events")
    data = fetch_or_stale(
        key="audit_events",
        context="감사 이벤트",
        fetcher=fetch_audit_events,
        args=(token,),
        kwargs={
            "page": int(audit_page_num),
            "size": int(f_page_size),
            "event_type": f_event_type.strip() or None,
            "user_id": f_user_id.strip() or None,
            "since": since_str,
            "scope": f_scope.strip() or None,
            "request_id": f_request_id.strip() or None,
        },
    )
    if data is None:
        return

    items: list[dict[str, Any]] = data.get("items", [])
    total: int = data.get("total", 0)
    total_pages = max(1, (total + int(f_page_size) - 1) // int(f_page_size))
    st.caption(f"Page {int(audit_page_num)} of {total_pages} — {total} total events")

    if not items:
        st.info("No audit events found.")
        return

    for _idx, event in enumerate(items):
        outcome: str = event.get("outcome", "")
        icon = "🔴" if outcome in ("deny", "denied") else "🟢"
        label = (
            f"{icon} [{event.get('created_at', '')[:19]}] "
            f"{event.get('event_type', 'unknown')} — "
            f"user: {str(event.get('user_id', ''))[:8]}… "
            f"tool: {event.get('tool_name', '—')}"
        )
        with st.expander(label, expanded=False):
            st.json(event)


_render_audit()
