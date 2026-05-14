"""HTTP API client for the Pyrene MCP frontend.

Mirrors `pyrene-dashboard.api_client` design (sync httpx, @st.cache_resource
singleton, @st.cache_data per call, friendly_error + fetch_or_stale UX 5각형
embedded). Helpers are intentionally duplicated rather than imported from
`pyrene-dashboard` because ADR-019 forbids cross-package imports between
service-layer packages — extraction to a shared `pyrene-ui-utils` module
is deferred to v2 (PRD-040 Out of scope §"shared helper extraction").
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx
import streamlit as st

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "http://localhost:8000"
_DEFAULT_LOGFIRE_URL = "https://logfire.pydantic.dev"


def get_base_url() -> str:
    return os.environ.get("PYRENE_API_URL", _DEFAULT_BASE_URL)


def get_logfire_base_url() -> str:
    return os.environ.get("LOGFIRE_URL", _DEFAULT_LOGFIRE_URL)


# ---------------------------------------------------------------------------
# Singleton client
# ---------------------------------------------------------------------------


@st.cache_resource
def get_client() -> httpx.Client:
    """Shared sync httpx.Client. Cleared only on Streamlit worker restart."""
    return httpx.Client(
        base_url=get_base_url(),
        timeout=httpx.Timeout(30.0),  # invoke can be slower than dashboard reads
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Friendly error mapping (PRD-020 mirror)
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
    422: "입력값이 올바르지 않습니다 — 인자를 확인하세요",
}


def friendly_error(exc: BaseException, context: str = "데이터") -> str:
    """원본 예외를 한국어 + 다음 행동 메시지로 매핑."""
    for exc_type, message in _FRIENDLY_BY_TYPE:
        if isinstance(exc, exc_type):
            return (
                f"{context}을(를) 불러올 수 없습니다 — {message} "
                f"(원인: {type(exc).__name__})"
            )
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
    return f"{context}을(를) 불러올 수 없습니다 (원인: {type(exc).__name__})"


# ---------------------------------------------------------------------------
# Stale-while-error fallback (PRD-032 / ADR-018 mirror)
# ---------------------------------------------------------------------------


def format_age_korean(ts: float | None) -> str:
    if ts is None:
        return "방금"
    delta = time.time() - ts
    if delta < 1:
        return "방금"
    if delta < 60:
        return f"{int(delta)}초 전"
    if delta < 3600:
        return f"{int(delta // 60)}분 전"
    return f"{int(delta // 3600)}시간 전"


def fetch_or_stale[T](
    *,
    key: str,
    context: str,
    fetcher: Callable[..., T],
    args: tuple[object, ...] = (),
    kwargs: dict[str, object] | None = None,
) -> T | None:
    """spinner + friendly_error + 🔄 재시도 + stale-while-error 단일 헬퍼.

    `code-style.md` §"외부 의존 fetch UX 오각형" invariant. Mirrors
    `pyrene-dashboard.api_client.fetch_or_stale`.
    """
    kwargs = kwargs or {}
    try:
        with st.spinner("최신 데이터 동기화 중…", show_time=False):
            data = fetcher(*args, **kwargs)
        st.session_state[f"_stale_{key}"] = (data, time.time())
        return data
    except Exception as exc:
        cached = st.session_state.get(f"_stale_{key}")
        if cached is not None:
            cached_data, cached_ts = cached
            st.warning(
                f"⚠️ 데이터 갱신 실패 — 마지막 갱신 {format_age_korean(cached_ts)}"
            )
            return cached_data  # type: ignore[no-any-return]
        st.error(friendly_error(exc, context=context))
        if st.button("🔄 재시도", key=f"retry_{key}"):
            fetcher.clear()  # type: ignore[attr-defined]  # @st.cache_data .clear()
            st.rerun(scope="fragment")
        return None


# ---------------------------------------------------------------------------
# /auth/me — used by auth gate
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_me(token: str) -> dict[str, Any]:
    client = get_client()
    r = client.get("/auth/me", headers=_auth_headers(token))
    r.raise_for_status()
    return dict(r.json())


# ---------------------------------------------------------------------------
# /gateway/servers — list
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_servers(token: str) -> list[dict[str, Any]]:
    client = get_client()
    r = client.get("/gateway/servers", headers=_auth_headers(token))
    r.raise_for_status()
    result: list[dict[str, Any]] = r.json()
    return result


# ---------------------------------------------------------------------------
# /gateway/servers/{id}/tools — list discovered tools
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_tools(token: str, server_id: str) -> list[dict[str, Any]]:
    client = get_client()
    r = client.get(
        f"/gateway/servers/{server_id}/tools",
        headers=_auth_headers(token),
    )
    r.raise_for_status()
    result: list[dict[str, Any]] = r.json()
    return result


# ---------------------------------------------------------------------------
# /gateway/servers/{id}/tools/{name}/invoke — PRD-040 Wave 1
# ---------------------------------------------------------------------------


def invoke_tool(
    token: str,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """POST /gateway/servers/{id}/tools/{name}/invoke.

    NOT cached: invocation has side-effects (audit row, budget charge).
    """
    client = get_client()
    r = client.post(
        f"/gateway/servers/{server_id}/tools/{tool_name}/invoke",
        headers=_auth_headers(token),
        json={"arguments": arguments},
    )
    r.raise_for_status()
    return dict(r.json())


# ---------------------------------------------------------------------------
# Logfire deep link helper (F-12 signal)
# ---------------------------------------------------------------------------


def logfire_trace_url(trace_id: str) -> str | None:
    """Build a Logfire dashboard URL for the given trace id, or None when
    the trace id is empty (no recording context)."""
    if not trace_id:
        return None
    return f"{get_logfire_base_url().rstrip('/')}/traces/{trace_id}"


__all__ = [
    "fetch_me",
    "fetch_or_stale",
    "fetch_servers",
    "fetch_tools",
    "format_age_korean",
    "friendly_error",
    "get_base_url",
    "get_client",
    "get_logfire_base_url",
    "invoke_tool",
    "logfire_trace_url",
]
