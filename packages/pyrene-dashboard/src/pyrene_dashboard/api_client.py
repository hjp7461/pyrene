"""HTTP API client for the Pyrene admin dashboard.

Design decisions (PM Wave 0' amend):
- ``httpx.Client`` (sync) — avoids Streamlit / asyncio event-loop collision.
- ``@st.cache_resource`` — single ``httpx.Client`` instance shared across all
  Streamlit sessions in the same worker process.
- ``@st.cache_data(ttl=30)`` — per-call caching; paired with
  ``st.fragment(run_every=30)`` for polling pages.
- No matplotlib: plotly-compatible dict payloads only.

Usage
-----
    from pyrene_dashboard.api_client import (
        get_client,
        fetch_rbac_matrix,
        fetch_denials_last_hour,
        fetch_budget_blocked,
        fetch_usage_summary,
        fetch_usage_records,
        fetch_audit_events,
        fetch_audit_timeline,
        fetch_data_permissions,
        fetch_budgets,
        fetch_me,
    )
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "http://localhost:8000"


def get_base_url() -> str:
    return os.environ.get("PYRENE_API_URL", _DEFAULT_BASE_URL)


# ---------------------------------------------------------------------------
# Singleton client (cache_resource = one client per Streamlit worker process)
# ---------------------------------------------------------------------------


@st.cache_resource
def get_client() -> httpx.Client:
    """Return the shared sync httpx.Client.

    ``@st.cache_resource`` is cleared only on server restart.
    The client does *not* carry auth headers — each call must pass
    ``headers={"Authorization": "Bearer <token>"}`` explicitly so that
    different admin sessions can use different tokens without sharing state.
    """
    return httpx.Client(
        base_url=get_base_url(),
        timeout=httpx.Timeout(10.0),
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Friendly error mapping (PRD-020 F-1, F-2)
# ---------------------------------------------------------------------------

_FRIENDLY_BY_TYPE: tuple[tuple[type[BaseException], str], ...] = (
    (
        httpx.ConnectError,
        "서버에 연결할 수 없습니다 — 잠시 후 다시 시도하거나 관리자에게 문의하세요",
    ),
    (httpx.ReadTimeout, "서버 응답이 지연됩니다 — 잠시 후 다시 시도하세요"),
    (httpx.WriteTimeout, "서버 응답이 지연됩니다 — 잠시 후 다시 시도하세요"),
)

_FRIENDLY_BY_STATUS: dict[int, str] = {
    401: "인증이 만료되었습니다 — 로그아웃 후 다시 로그인하세요",
    403: "접근 권한이 없습니다 — 관리자에게 권한 요청을 보내세요",
    404: "해당 리소스를 찾을 수 없습니다 — 입력값을 확인하세요",
}


def friendly_error(exc: BaseException, context: str = "데이터") -> str:
    """PRD-020: 영문 raw exception 을 사용자 언어 + 다음 행동 메시지로 매핑.

    Args:
        exc: 원본 예외.
        context: 사용자 friendly 컨텍스트 ("RBAC 매트릭스", "감사 이벤트" 등).

    Returns:
        한국어 메시지. 원인 타입은 `(원인: ExcName)` 또는 `(HTTP {status})` 형태로
        부분 노출 — 디버깅 친화 + 신뢰감 (PRD-020 Open Question Q1).
    """
    # 1) 타입 기반 매핑 (httpx 전송 계층)
    for exc_type, message in _FRIENDLY_BY_TYPE:
        if isinstance(exc, exc_type):
            return (
                f"{context}을(를) 불러올 수 없습니다 — {message} "
                f"(원인: {type(exc).__name__})"
            )

    # 2) HTTP 상태 기반 매핑
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in _FRIENDLY_BY_STATUS:
            return f"{context}: {_FRIENDLY_BY_STATUS[status]} (HTTP {status})"
        if 500 <= status < 600:
            return (
                f"{context}: 서버 오류가 발생했습니다 — 관리자에게 문의하세요 "
                f"(HTTP {status})"
            )
        if 400 <= status < 500:
            return (
                f"{context}: 요청을 처리할 수 없습니다 — 입력값을 확인하세요 "
                f"(HTTP {status})"
            )

    # 3) fallback
    return f"{context}을(를) 불러올 수 없습니다 (원인: {type(exc).__name__})"


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_me(token: str) -> dict[str, Any]:
    """GET /auth/me — returns the current user profile (roles, team_id, …)."""
    client = get_client()
    r = client.get("/auth/me", headers=_auth_headers(token))
    r.raise_for_status()
    return dict(r.json())


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


__all__ = [
    "fetch_audit_events",
    "fetch_audit_timeline",
    "fetch_budget_blocked",
    "fetch_budgets",
    "fetch_data_permissions",
    "fetch_denials_last_hour",
    "fetch_me",
    "fetch_rbac_matrix",
    "fetch_usage_records",
    "fetch_usage_summary",
    "get_base_url",
    "get_client",
]
