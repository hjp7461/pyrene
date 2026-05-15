"""pyrene-ui-common 헬퍼 단위 가드 (PRD-051 / ADR-025).

dashboard ↔ mcp-frontend 에서 추출된 7 helper 의 동작 + parameterize 분기
(get_client timeout / friendly_error extra_status) 검증. st 의존 helper 는
`pyrene_ui_common.http.st` 를 patch (helper 정의 모듈 기준).
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from pyrene_ui_common import (
    fetch_or_stale,
    format_age_korean,
    friendly_error,
    get_base_url,
    get_client,
)

# ---------------------------------------------------------------------------
# get_base_url / get_client (parameterize 분기 — PRD-051 핵심)
# ---------------------------------------------------------------------------


class TestClient:
    def test_get_base_url_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert get_base_url() == "http://localhost:8000"

    def test_get_base_url_env_override(self) -> None:
        with patch.dict("os.environ", {"PYRENE_API_URL": "http://x:9"}, clear=True):
            assert get_base_url() == "http://x:9"

    def test_get_client_default_timeout_10(self) -> None:
        get_client.clear()
        client = get_client()
        assert client.timeout.read == 10.0

    def test_get_client_param_timeout_30(self) -> None:
        get_client.clear()
        client = get_client(timeout=30.0)
        assert client.timeout.read == 30.0


# ---------------------------------------------------------------------------
# friendly_error — base + extra_status 병합 분기
# ---------------------------------------------------------------------------


class TestFriendlyError:
    def test_connect_error_korean(self) -> None:
        msg = friendly_error(httpx.ConnectError("x"), context="도구 호출")
        assert "서버에 연결할 수 없습니다" in msg
        assert "(원인: ConnectError)" in msg

    def test_http_403_base(self) -> None:
        resp = httpx.Response(403, request=httpx.Request("GET", "http://x"))
        exc = httpx.HTTPStatusError("403", request=resp.request, response=resp)
        assert "접근 권한이 없습니다" in friendly_error(exc, context="X")

    def test_http_422_falls_to_generic_without_extra(self) -> None:
        """extra_status 없으면 422 는 generic 4xx (dashboard 동작 보존)."""
        resp = httpx.Response(422, request=httpx.Request("GET", "http://x"))
        exc = httpx.HTTPStatusError("422", request=resp.request, response=resp)
        msg = friendly_error(exc, context="X")
        assert "요청을 처리할 수 없습니다" in msg
        assert "(HTTP 422)" in msg

    def test_http_422_with_extra_status(self) -> None:
        """extra_status={422:...} 주면 specific 메시지 (mcp-frontend 동작 보존)."""
        resp = httpx.Response(422, request=httpx.Request("GET", "http://x"))
        exc = httpx.HTTPStatusError("422", request=resp.request, response=resp)
        msg = friendly_error(
            exc,
            context="X",
            extra_status={422: "입력값이 올바르지 않습니다 — 인자를 확인하세요"},
        )
        assert "입력값이 올바르지 않습니다" in msg
        assert "(HTTP 422)" in msg

    def test_5xx_korean(self) -> None:
        resp = httpx.Response(503, request=httpx.Request("GET", "http://x"))
        exc = httpx.HTTPStatusError("503", request=resp.request, response=resp)
        assert "서버 오류가 발생했습니다" in friendly_error(exc, context="X")

    def test_unknown_fallback(self) -> None:
        assert "(원인: ValueError)" in friendly_error(ValueError("x"), context="X")


# ---------------------------------------------------------------------------
# format_age_korean
# ---------------------------------------------------------------------------


class TestFormatAgeKorean:
    def test_none_returns_now(self) -> None:
        assert format_age_korean(None) == "방금"

    def test_sub_second_returns_now(self) -> None:
        assert format_age_korean(time.time()) == "방금"

    def test_seconds(self) -> None:
        assert format_age_korean(time.time() - 30).endswith("초 전")

    def test_minutes(self) -> None:
        assert format_age_korean(time.time() - 300).endswith("분 전")

    def test_hours(self) -> None:
        assert format_age_korean(time.time() - 7200).endswith("시간 전")


# ---------------------------------------------------------------------------
# fetch_or_stale 3 분기 (st 는 pyrene_ui_common.http 모듈 기준 patch)
# ---------------------------------------------------------------------------


class TestFetchOrStale:
    def test_success_caches(self) -> None:
        state: dict[str, Any] = {}
        fetcher = MagicMock(return_value={"ok": True})
        with (
            patch("pyrene_ui_common.http.st.session_state", state),
            patch("pyrene_ui_common.http.st.spinner") as sp,
        ):
            sp.return_value.__enter__ = MagicMock(return_value=None)
            sp.return_value.__exit__ = MagicMock(return_value=False)
            result = fetch_or_stale(key="t", context="테스트", fetcher=fetcher)
        assert result == {"ok": True}
        assert state["_stale_t"][0] == {"ok": True}

    def test_failure_with_cache_warns(self) -> None:
        state: dict[str, Any] = {"_stale_t": ({"cached": 1}, time.time() - 60)}
        fetcher = MagicMock(side_effect=RuntimeError("fail"))
        with (
            patch("pyrene_ui_common.http.st.session_state", state),
            patch("pyrene_ui_common.http.st.spinner") as sp,
            patch("pyrene_ui_common.http.st.warning") as warn,
            patch("pyrene_ui_common.http.st.error") as err,
        ):
            sp.return_value.__enter__ = MagicMock(return_value=None)
            sp.return_value.__exit__ = MagicMock(return_value=False)
            result = fetch_or_stale(key="t", context="테스트", fetcher=fetcher)
        assert result == {"cached": 1}
        warn.assert_called_once()
        err.assert_not_called()

    def test_failure_without_cache_errors(self) -> None:
        state: dict[str, Any] = {}
        fetcher = MagicMock(side_effect=RuntimeError("fail"))
        fetcher.clear = MagicMock()
        with (
            patch("pyrene_ui_common.http.st.session_state", state),
            patch("pyrene_ui_common.http.st.spinner") as sp,
            patch("pyrene_ui_common.http.st.warning") as warn,
            patch("pyrene_ui_common.http.st.error") as err,
            patch("pyrene_ui_common.http.st.button", return_value=False) as btn,
        ):
            sp.return_value.__enter__ = MagicMock(return_value=None)
            sp.return_value.__exit__ = MagicMock(return_value=False)
            result = fetch_or_stale(key="t", context="테스트", fetcher=fetcher)
        assert result is None
        warn.assert_not_called()
        err.assert_called_once()
        assert btn.call_args.kwargs.get("key") == "retry_t"
