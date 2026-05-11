"""Page 3 — RBAC Matrix (read-only).

Two sections:
  1. Role x Tool matrix from /rbac/matrix (PLAN-010)
  2. Data permissions from /rbac/data-permissions (PLAN-011)

Color palette (PRD-016 Day 3 unified):
  allow = green  (#22c55e)
  deny  = red    (#ef4444)

Auto-refresh: @st.fragment(run_every=30)
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from pyrene_dashboard import auth
from pyrene_dashboard.api_client import (
    fetch_data_permissions,
    fetch_rbac_matrix,
    friendly_error,
)

_GREEN = "#22c55e"
_RED = "#ef4444"
_AMBER = "#f59e0b"

st.title("RBAC Matrix")

token = auth.require_admin()


def _color_action(val: str) -> str:
    low = str(val).lower()
    if low == "allow":
        return f"background-color: {_GREEN}; color: white"
    if low == "deny":
        return f"background-color: {_RED}; color: white"
    return f"background-color: {_AMBER}; color: white"


@st.fragment(run_every=30)
def _render_rbac_matrix() -> None:
    """Fetch and display the full Role x Tool matrix."""
    st.subheader("Role x Tool Permissions")
    try:
        matrix = fetch_rbac_matrix(token)
        roles: list[dict[str, Any]] = matrix.get("roles", [])
        tools: list[str] = matrix.get("tools", [])

        if not roles:
            st.info("No RBAC permissions configured.")
            return

        rows = []
        for role_entry in roles:
            role_name: str = role_entry.get("role_name", role_entry.get("role_id", "?"))
            tool_map: dict[str, str] = role_entry.get("tools", {})
            row: dict[str, str] = {"Role": role_name}
            for tool in tools:
                row[tool] = tool_map.get(tool, "deny")
            rows.append(row)

        df = pd.DataFrame(rows).set_index("Role")

        # Apply color styling to all data columns
        styled = df.style.map(_color_action)  # type: ignore[arg-type]
        st.dataframe(styled, use_container_width=True)
        st.caption(
            f"{len(roles)} role(s) x {len(tools)} tool(s) — read-only."
            " Use the API or CLI to modify permissions."
        )
    except Exception as exc:
        st.error(friendly_error(exc, context="RBAC 매트릭스"))


@st.fragment(run_every=30)
def _render_data_permissions() -> None:
    """Fetch and display data-level permissions (connection/schema/table)."""
    st.subheader("Data Permissions (Role x Connection / Schema / Table)")
    try:
        data = fetch_data_permissions(token, size=100)
        items: list[dict[str, Any]] = (
            data if isinstance(data, list) else data.get("items", [])
        )

        if not items:
            st.info("No data-level permissions configured.")
            return

        rows = []
        for perm in items:
            rows.append(
                {
                    "Role ID": str(perm.get("role_id", ""))[:8] + "…",
                    "Connection": str(perm.get("connection_id", ""))[:8] + "…",
                    "Schema": perm.get("schema", "*"),
                    "Table": perm.get("table", "*"),
                    "Action": perm.get("action", "deny"),
                    "Admin grant": "Yes" if perm.get("is_admin_grant") else "No",
                }
            )

        df = pd.DataFrame(rows)

        def _color_row(s: pd.Series[str]) -> list[str]:
            action = s.get("Action", "deny")
            bg = _GREEN if action == "allow" else _RED
            return [
                f"background-color: {bg}; color: white" if col == "Action" else ""
                for col in s.index
            ]

        styled = df.style.apply(_color_row, axis=1)
        st.dataframe(styled, use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(friendly_error(exc, context="데이터 권한"))


_render_rbac_matrix()
st.divider()
_render_data_permissions()
