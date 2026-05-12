"""Page 2 — Usage by user/agent.

Features (PM Wave 0' amend):
- Server-side paging: ``?page=X&size=Y&order_by=...`` forwarded to the API.
- ``st.dataframe`` + manual paging UI (``st.number_input``, ``st.selectbox``).
- Filter UI inside ``st.form`` with "Apply" submit — prevents live reruns on
  every widget change.
- Timezone: all timestamps UTC-normalized; displayed via ``pendulum``
  local conversion.
- Auto-refresh: ``@st.fragment(run_every=30)`` wraps only the data region.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pendulum
import streamlit as st

from pyrene_dashboard import auth
from pyrene_dashboard.api_client import fetch_usage_records, friendly_error

st.title("Usage by User / Agent")

token = auth.require_admin()

# ---------------------------------------------------------------------------
# Filter form (st.form prevents live reruns on widget changes)
# ---------------------------------------------------------------------------

with st.form("usage_filter_form"):
    st.subheader("Filters")
    col1, col2, col3 = st.columns(3)
    with col1:
        filter_user_id = st.text_input("User ID (UUID)", key="usage_user_id")
    with col2:
        filter_agent_id = st.text_input("Agent ID (UUID)", key="usage_agent_id")
    with col3:
        filter_since = st.date_input("Since (UTC date)", value=None, key="usage_since")

    filter_until = st.date_input("Until (UTC date)", value=None, key="usage_until")

    order_by_options = ["created_at", "cost_usd", "input_tokens", "output_tokens"]
    filter_order = st.selectbox("Order by", order_by_options, key="usage_order")

    submitted = st.form_submit_button("Apply")

# ---------------------------------------------------------------------------
# Paging controls (outside form — changes page does NOT rerun filters)
# ---------------------------------------------------------------------------

page_size_options = [10, 25, 50, 100]
page_size = st.selectbox("Rows per page", page_size_options, index=1, key="usage_page_size")
page_number = st.number_input("Page", min_value=1, value=1, step=1, key="usage_page_num")


# ---------------------------------------------------------------------------
# Data region: auto-refreshed every 30 s
# ---------------------------------------------------------------------------


@st.fragment(run_every=30)
def _render_usage_table() -> None:
    """Fetch + display usage records with server-side paging."""
    since_str: str | None = None
    until_str: str | None = None

    if filter_since:
        since_str = pendulum.instance(
            pendulum.datetime(filter_since.year, filter_since.month, filter_since.day, tz="UTC")
        ).to_iso8601_string()
    if filter_until:
        until_str = pendulum.instance(
            pendulum.datetime(filter_until.year, filter_until.month, filter_until.day, tz="UTC")
        ).to_iso8601_string()

    try:
        with st.spinner("최신 데이터 동기화 중…", show_time=False):
            data = fetch_usage_records(
                token,
                page=int(page_number),
                size=int(page_size),
                order_by=str(filter_order),
                user_id=filter_user_id.strip() or None,
                agent_id=filter_agent_id.strip() or None,
                since=since_str,
                until=until_str,
            )
    except Exception as exc:
        st.error(friendly_error(exc, context="사용량 레코드"))
        if st.button("🔄 재시도", key="retry_usage_records"):
            fetch_usage_records.clear()
            st.rerun(scope="fragment")
        return

    items: list[dict[str, Any]] = data.get("items", [])
    total: int = data.get("total", 0)
    total_pages = max(1, (total + int(page_size) - 1) // int(page_size))

    st.caption(f"Page {int(page_number)} of {total_pages} — {total} total records")

    if not items:
        st.info("No usage records found.")
        return

    # Normalize timestamps to local timezone via pendulum
    local_tz = pendulum.local_timezone()  # type: ignore[operator,unused-ignore]
    rows = []
    for rec in items:
        created_utc_str: str = rec.get("created_at", "")
        try:
            if created_utc_str:
                parsed = pendulum.parse(created_utc_str, tz="UTC")
                if hasattr(parsed, "in_timezone"):
                    created_local = parsed.in_timezone(local_tz).to_datetime_string()
                else:
                    created_local = created_utc_str
            else:
                created_local = ""
        except Exception:
            created_local = created_utc_str

        rows.append(
            {
                "Created (local)": created_local,
                "User": str(rec.get("user_id", ""))[:8] + "…",
                "Agent": str(rec.get("agent_id", ""))[:8] + "…" if rec.get("agent_id") else "—",
                "Model": rec.get("model", ""),
                "Input tok.": rec.get("input_tokens", 0),
                "Output tok.": rec.get("output_tokens", 0),
                "Cost USD": f"{float(rec.get('cost_usd', 0)):.6f}",
            }
        )

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


_render_usage_table()
