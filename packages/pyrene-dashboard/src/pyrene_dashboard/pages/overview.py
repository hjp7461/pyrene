"""Page 1 — Overview (PM Wave 0' amend redesign).

3-tile upper layout:
  Tile 1: RBAC heatmap (4 roles x 4 tools) — st.dataframe + color cells
  Tile 2: "Denials in last 1h" big counter (st.metric) + recent 5 rows
  Tile 3: "Budget-blocked requests" big counter + daily trend mini-chart

Lower section:
  - Daily cost line chart (st.line_chart)
  - Active users count (st.metric)

Auto-refresh: st.fragment(run_every=30) wraps the data tiles so only the
data region reruns every 30 s — the page chrome stays stable.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from pyrene_dashboard import auth
from pyrene_dashboard.api_client import (
    fetch_budget_blocked,
    fetch_denials_last_hour,
    fetch_rbac_matrix,
    fetch_usage_summary,
    friendly_error,
)

# ---------------------------------------------------------------------------
# Color palette (PRD-016 Day 3 unified palette)
# ---------------------------------------------------------------------------
_GREEN = "#22c55e"
_RED = "#ef4444"
_AMBER = "#f59e0b"

st.title("Overview")

token = auth.require_admin()


# ---------------------------------------------------------------------------
# Helper: build RBAC heatmap DataFrame with color styling
# ---------------------------------------------------------------------------


def _build_heatmap_df(matrix: dict[str, Any]) -> pd.DataFrame:
    """Convert /rbac/matrix response to a styled 2D DataFrame."""
    roles: list[dict[str, Any]] = matrix.get("roles", [])
    tools: list[str] = matrix.get("tools", [])

    if not roles:
        return pd.DataFrame()

    # Clamp to max 4 x 4 for the overview tile
    display_tools = tools[:4]
    display_roles = roles[:4]

    rows = []
    for role_entry in display_roles:
        role_name: str = role_entry.get("role_name", role_entry.get("role_id", "?"))
        tool_map: dict[str, str] = role_entry.get("tools", {})
        row: dict[str, str] = {"Role": role_name}
        for tool in display_tools:
            row[tool] = tool_map.get(tool, "deny")
        rows.append(row)

    df = pd.DataFrame(rows).set_index("Role")
    return df


def _color_cell(val: str) -> str:
    """Return CSS background-color style for a single cell value."""
    low = str(val).lower()
    if low == "allow":
        return f"background-color: {_GREEN}; color: white"
    if low == "deny":
        return f"background-color: {_RED}; color: white"
    return f"background-color: {_AMBER}; color: white"


# ---------------------------------------------------------------------------
# Fragment: data tiles (auto-refreshed every 30 s)
# ---------------------------------------------------------------------------


@st.fragment(run_every=30)
def _render_data_tiles() -> None:
    """Render the 3-tile layout + lower charts, refreshed every 30 s."""
    tile1, tile2, tile3 = st.columns(3)

    # ---- Tile 1: RBAC Heatmap ----
    with tile1:
        st.subheader("RBAC Heatmap")
        try:
            matrix = fetch_rbac_matrix(token)
            heatmap_df = _build_heatmap_df(matrix)
            if heatmap_df.empty:
                st.info("No RBAC matrix data available.")
            else:
                styled = heatmap_df.style.map(_color_cell)  # type: ignore[arg-type]
                st.dataframe(styled, use_container_width=True)
                all_tools: list[str] = matrix.get("tools", [])
                if len(all_tools) > 4:
                    st.caption(
                        f"Showing 4 of {len(all_tools)} tools."
                        " See RBAC Matrix page for full view."
                    )
        except Exception as exc:
            st.error(friendly_error(exc, context="RBAC 매트릭스"))

    # ---- Tile 2: Denials in last 1h ----
    with tile2:
        st.subheader("Denials — last 1h")
        try:
            denial_data = fetch_denials_last_hour(token)
            denial_count: int = denial_data.get("count", 0)
            recent_denials: list[dict[str, Any]] = denial_data.get("recent", [])

            st.metric(
                label="Total denials",
                value=denial_count,
                delta=None,
                help="RBAC deny decisions in the last 60 minutes",
            )

            if recent_denials:
                rows = [
                    {
                        "Time": r.get("created_at", "")[:19],
                        "Type": r.get("event_type", ""),
                        "User": str(r.get("user_id", ""))[:8],
                        "Tool": r.get("tool_name", ""),
                        "Outcome": r.get("outcome", ""),
                    }
                    for r in recent_denials
                ]
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            else:
                st.success("No denials in the last hour.")
        except Exception as exc:
            st.error(friendly_error(exc, context="거부 카운터"))

    # ---- Tile 3: Budget-blocked requests ----
    with tile3:
        st.subheader("Budget-Blocked Requests")
        try:
            blocked_data = fetch_budget_blocked(token)
            blocked_count: int = blocked_data.get("count", 0)
            trend: list[dict[str, Any]] = blocked_data.get("trend", [])

            st.metric(
                label="Budget-blocked requests",
                value=blocked_count,
                delta=None,
                help="Requests rejected due to budget exhaustion",
            )

            if trend:
                trend_df = pd.DataFrame(trend).set_index("date")
                st.line_chart(trend_df["count"], height=150, use_container_width=True)
            else:
                st.caption("No blocked requests recorded.")
        except Exception as exc:
            st.error(friendly_error(exc, context="예산 차단 데이터"))

    # ---- Lower section ----
    st.divider()
    lower_left, lower_right = st.columns([3, 1])

    with lower_left:
        st.subheader("Daily Cost (USD)")
        try:
            summaries = fetch_usage_summary(token, period="day")
            if summaries:
                cost_df = pd.DataFrame(
                    [
                        {
                            "date": s.get("period_label", ""),
                            "cost_usd": float(s.get("total_cost_usd", 0)),
                        }
                        for s in summaries
                    ]
                ).set_index("date")
                st.line_chart(cost_df["cost_usd"], use_container_width=True)
            else:
                st.info("No usage data available.")
        except Exception as exc:
            st.error(friendly_error(exc, context="사용량 요약"))

    with lower_right:
        st.subheader("Active Users")
        try:
            summaries = fetch_usage_summary(token, period="day")
            if summaries and summaries:
                latest = summaries[-1]
                active_users: int = latest.get("request_count", 0)
                st.metric(
                    label="Requests today",
                    value=active_users,
                    help="Total request count in the most recent day bucket",
                )
        except Exception as exc:
            st.error(friendly_error(exc, context="활성 사용자 수"))


_render_data_tiles()
