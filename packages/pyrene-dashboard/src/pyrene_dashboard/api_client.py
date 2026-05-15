"""HTTP API client for the Pyrene admin dashboard.

Generic HTTP + UX helpers (``get_base_url``, ``get_client``, ``_auth_headers``,
``friendly_error``, ``format_age_korean``, ``fetch_or_stale``, ``fetch_me``) are
re-exported from ``pyrene-ui-common`` (ADR-025 leaf-utility exemption) and no
longer duplicated here. This module keeps only the dashboard-specific domain
fetchers (rbac / data-permissions / audit / metering / budgets).

Design decisions (PM Wave 0' amend):
- ``httpx.Client`` (sync) — avoids Streamlit / asyncio event-loop collision.
- ``@st.cache_resource`` — single client per Streamlit worker process.
- ``@st.cache_data(ttl=30)`` — per-call caching; paired with
  ``st.fragment(run_every=30)`` for polling pages.
- dashboard uses the default 10s client timeout (``get_client()``).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from pyrene_ui_common import (
    _auth_headers,
    fetch_me,
    fetch_or_stale,
    format_age_korean,
    friendly_error,
    get_base_url,
    get_client,
)

# ---------------------------------------------------------------------------
# /rbac/matrix
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_rbac_matrix(token: str) -> dict[str, Any]:
    """GET /rbac/matrix — Role x Tool 2D matrix snapshot."""
    client = get_client()
    r = client.get("/rbac/matrix", headers=_auth_headers(token))
    r.raise_for_status()
    return dict(r.json())


# ---------------------------------------------------------------------------
# /rbac/data-permissions (data-RBAC)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_data_permissions(
    token: str,
    *,
    page: int = 1,
    size: int = 50,
) -> dict[str, Any]:
    """GET /rbac/data-permissions — paginated data-permission rows."""
    client = get_client()
    r = client.get(
        "/rbac/data-permissions",
        params={"page": page, "size": size},
        headers=_auth_headers(token),
    )
    r.raise_for_status()
    return dict(r.json())


# ---------------------------------------------------------------------------
# /audit/events
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_audit_events(
    token: str,
    *,
    page: int = 1,
    size: int = 25,
    event_type: str | None = None,
    user_id: str | None = None,
    since: str | None = None,
    scope: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """GET /audit/events — paginated audit log with optional filters."""
    params: dict[str, Any] = {"page": page, "size": size}
    if event_type:
        params["event_type"] = event_type
    if user_id:
        params["user_id"] = user_id
    if since:
        params["since"] = since
    if scope:
        params["scope"] = scope
    if request_id:
        params["request_id"] = request_id

    client = get_client()
    r = client.get("/audit/events", params=params, headers=_auth_headers(token))
    r.raise_for_status()
    return dict(r.json())


@st.cache_data(ttl=30)
def fetch_audit_timeline(
    token: str,
    *,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """GET /audit/events/timeline — hourly bucket counts."""
    params: dict[str, str] = {}
    if since:
        params["since"] = since

    client = get_client()
    r = client.get("/audit/events/timeline", params=params, headers=_auth_headers(token))
    r.raise_for_status()
    result: list[dict[str, Any]] = r.json()
    return result


# ---------------------------------------------------------------------------
# /metering/usage (summary) + /metering/usage/records (paginated)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_usage_summary(
    token: str,
    *,
    period: str = "day",
) -> list[dict[str, Any]]:
    """GET /metering/usage — list of UsageSummary DTOs."""
    client = get_client()
    r = client.get(
        "/metering/usage",
        params={"period": period},
        headers=_auth_headers(token),
    )
    r.raise_for_status()
    result: list[dict[str, Any]] = r.json()
    return result


@st.cache_data(ttl=30)
def fetch_usage_records(
    token: str,
    *,
    page: int = 1,
    size: int = 25,
    order_by: str = "created_at",
    user_id: str | None = None,
    agent_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """GET /metering/usage/records — paginated usage records (server-side paging)."""
    params: dict[str, Any] = {"page": page, "size": size, "order_by": order_by}
    if user_id:
        params["user_id"] = user_id
    if agent_id:
        params["agent_id"] = agent_id
    if since:
        params["since"] = since
    if until:
        params["until"] = until

    client = get_client()
    r = client.get("/metering/usage/records", params=params, headers=_auth_headers(token))
    r.raise_for_status()
    return dict(r.json())


# ---------------------------------------------------------------------------
# Denial counter — derived from audit events (outcome=deny, last 1h)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_denials_last_hour(token: str) -> dict[str, Any]:
    """Return denial count + latest 5 rows from /audit/events for last 1h."""
    import pendulum  # local import keeps module-level imports clean

    since = pendulum.now("UTC").subtract(hours=1).to_iso8601_string()
    data = fetch_audit_events(token, size=100, since=since)
    items: list[dict[str, Any]] = data.get("items", [])
    denials = [row for row in items if row.get("outcome", "").lower() in ("deny", "denied")]
    return {"count": len(denials), "recent": denials[:5]}


# ---------------------------------------------------------------------------
# /budgets/*
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_budgets(token: str) -> list[dict[str, Any]]:
    """GET /budgets — list of BudgetLimitResponse."""
    client = get_client()
    r = client.get("/budgets", headers=_auth_headers(token))
    r.raise_for_status()
    result: list[dict[str, Any]] = r.json()
    return result


@st.cache_data(ttl=30)
def fetch_budget_blocked(token: str) -> dict[str, Any]:
    """Derive budget-blocked request count from audit events (outcome=budget_exceeded).

    Falls back to 0 if the endpoint returns nothing or fails.
    """
    try:
        data = fetch_audit_events(token, size=100, event_type="budget_exceeded")
        items: list[dict[str, Any]] = data.get("items", [])
        blocked_total: int = data.get("total", len(items))

        # Daily trend: count per date from the items we received
        from collections import defaultdict

        daily: dict[str, int] = defaultdict(int)
        for row in items:
            created = row.get("created_at", "")[:10]  # YYYY-MM-DD
            if created:
                daily[created] += 1

        trend = [{"date": d, "count": c} for d, c in sorted(daily.items())]
        return {"count": blocked_total, "trend": trend}
    except Exception:
        return {"count": 0, "trend": []}


# Public surface = re-exported ui-common helpers + dashboard domain fetchers.
# All listed explicitly for mypy --strict no_implicit_reexport (downstream
# pages / auth.py import the helpers from this module).
__all__ = [
    "_auth_headers",
    "fetch_audit_events",
    "fetch_audit_timeline",
    "fetch_budget_blocked",
    "fetch_budgets",
    "fetch_data_permissions",
    "fetch_denials_last_hour",
    "fetch_me",
    "fetch_or_stale",
    "fetch_rbac_matrix",
    "fetch_usage_records",
    "fetch_usage_summary",
    "format_age_korean",
    "friendly_error",
    "get_base_url",
    "get_client",
]
