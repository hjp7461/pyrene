"""Shared leaf-utility HTTP + UX helpers for Pyrene Streamlit frontends.

ADR-025 (leaf-utility exemption): this package depends on no domain package
(httpx / streamlit only) and is therefore exempt from the ADR-019 / F-15
service-layer cross-import ban. ``pyrene-dashboard`` and ``pyrene-mcp-frontend``
both import these helpers instead of duplicating them.

Two helpers carry a parameter to preserve each frontend's existing behavior
without forking the implementation:

- ``get_client(timeout=...)`` — dashboard uses the 10s default; the MCP
  frontend passes ``timeout=30.0`` (invoke can be slower than dashboard reads).
- ``friendly_error(..., extra_status=...)`` — the MCP frontend passes an
  ``{422: ...}`` mapping; the dashboard uses the base 401/403/404 set.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import streamlit as st

_DEFAULT_BASE_URL = "http://localhost:8000"


def get_base_url() -> str:
    return os.environ.get("PYRENE_API_URL", _DEFAULT_BASE_URL)


@st.cache_resource
def get_client(timeout: float = 10.0) -> httpx.Client:
    """Return the shared sync httpx.Client.

    ``@st.cache_resource`` keys on ``timeout``, so each distinct timeout is a
    distinct process-singleton client. The client does *not* carry auth
    headers — each call must pass ``headers={"Authorization": "Bearer <token>"}``
    explicitly so different sessions can use different tokens.
    """
    return httpx.Client(
        base_url=get_base_url(),
        timeout=httpx.Timeout(timeout),
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_FRIENDLY_BY_TYPE: tuple[tuple[type[BaseException], str], ...] = (
    (
        httpx.ConnectError,
        "서버에 연결할 수 없습니다 — 잠시 후 다시 시도하거나 관리자에게 문의하세요",
    ),
    (httpx.ReadTimeout, "서버 응답이 지연됩니다 — 잠시 후 다시 시도하세요"),
    (httpx.WriteTimeout, "서버 응답이 지연됩니다 — 잠시 후 다시 시도하세요"),
)

_FRIENDLY_BY_STATUS: Mapping[int, str] = {
    401: "인증이 만료되었습니다 — 로그아웃 후 다시 로그인하세요",
    403: "접근 권한이 없습니다 — 관리자에게 권한 요청을 보내세요",
    404: "해당 리소스를 찾을 수 없습니다 — 입력값을 확인하세요",
}


def friendly_error(
    exc: BaseException,
    context: str = "데이터",
    *,
    extra_status: Mapping[int, str] | None = None,
) -> str:
    """PRD-020: 영문 raw exception 을 사용자 언어 + 다음 행동 메시지로 매핑.

    Args:
        exc: 원본 예외.
        context: 사용자 friendly 컨텍스트 ("RBAC 매트릭스", "감사 이벤트" 등).
        extra_status: 호출 frontend 가 추가로 매핑할 HTTP status (예: MCP
            frontend 의 ``{422: ...}``). base 401/403/404 위에 병합.

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

    # 2) HTTP 상태 기반 매핑 (base 위에 호출자 extra 병합)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        status_map = (
            _FRIENDLY_BY_STATUS
            if extra_status is None
            else {**_FRIENDLY_BY_STATUS, **extra_status}
        )
        if status in status_map:
            return f"{context}: {status_map[status]} (HTTP {status})"
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


def format_age_korean(ts: float | None) -> str:
    """경과 시간을 한국어 상대 시간으로 포맷. PRD-032 / ADR-018.

    None → "방금" (cache-miss 또는 첫 fetch 직후 대비).
    """
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
    """Fetch with stale-while-error fallback. PRD-032 / ADR-018.

    3 분기:
    - 성공: session_state[f"_stale_{key}"] 캐시 갱신, 데이터 반환
    - 실패 + cache 있음: 노란 경고 + stale 데이터 반환
    - 실패 + cache 없음: 빨간 에러 + retry 버튼 + None 반환

    내부에 spinner / friendly_error / retry button 4 패턴 통합 (Do #8).
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
            fetcher.clear()  # type: ignore[attr-defined]  # @st.cache_data 의 .clear()
            st.rerun(scope="fragment")
        return None


@st.cache_data(ttl=30)
def fetch_me(token: str) -> dict[str, Any]:
    """GET /auth/me — returns the current user profile (roles, team_id, …)."""
    client = get_client()
    r = client.get("/auth/me", headers=_auth_headers(token))
    r.raise_for_status()
    return dict(r.json())
