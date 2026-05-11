"""PRD-024 회귀 가드: `_is_logfire_configured` 헬퍼.

LOGFIRE_URL 환경변수 / 모듈 상수 / 명시 인자가 *사용자 자기 trace* 로 설정됐는지
판정하는 헬퍼의 6+ 케이스 검증. 완전한 404/login 검출은 cross-origin iframe
sandbox 로 측정 불가 — 본 unit 은 *trivially detectable* 케이스만 가드.
참고: ADR-017.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch


def _import_traces_module() -> Any:
    """traces 페이지 모듈을 streamlit mocking 하에 import.

    Streamlit page 는 module top-level 에서 `st.title`, `auth.require_admin` 등을
    호출하므로 mock 필수. 이전 import 캐시 제거 후 re-import 해 env var 변화 반영.
    """
    for key in list(sys.modules.keys()):
        if "pyrene_dashboard.pages.traces" in key:
            del sys.modules[key]

    state = {
        "auth_token": "tok",
        "auth_role": ["admin"],
        "auth_expires_at": 9999999999.0,
    }
    patches: list[Any] = [
        patch("streamlit.session_state", state),
        patch("streamlit.fragment", side_effect=lambda **_kw: (lambda f: f)),
        patch("streamlit.title"),
        patch("streamlit.subheader"),
        patch("streamlit.caption"),
        patch("streamlit.divider"),
        patch("streamlit.info"),
        patch("streamlit.error"),
        patch("streamlit.warning"),
        patch("streamlit.columns", return_value=[MagicMock(), MagicMock()]),
        patch("streamlit.link_button"),
        patch(
            "streamlit.expander",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=None),
                __exit__=MagicMock(return_value=False),
            ),
        ),
        patch("streamlit.spinner", return_value=MagicMock(
            __enter__=MagicMock(return_value=None),
            __exit__=MagicMock(return_value=False),
        )),
        patch("streamlit.components.v1.iframe"),
        patch("pyrene_dashboard.auth.require_admin", return_value="tok"),
        patch(
            "pyrene_dashboard.api_client.fetch_audit_events",
            return_value={"items": [], "total": 0, "page": 1, "size": 5},
        ),
    ]
    for p in patches:
        p.start()
    try:
        import pyrene_dashboard.pages.traces as traces_module
        return traces_module
    finally:
        for p in patches:
            p.stop()


class TestIsLogfireConfigured:
    """`_is_logfire_configured(url)` heuristic — explicit url 인자."""

    def test_empty_string_is_not_configured_detection(self) -> None:
        """빈 문자열 → 미연동."""
        traces = _import_traces_module()
        assert traces._is_logfire_configured("") is False

    def test_default_url_is_not_configured_detection(self) -> None:
        """기본값 (Logfire 홈/login) → 미연동."""
        traces = _import_traces_module()
        assert traces._is_logfire_configured("https://logfire.pydantic.dev") is False

    def test_default_with_trailing_slash_is_not_configured_detection(self) -> None:
        """trailing slash 변형 흡수 → 미연동."""
        traces = _import_traces_module()
        assert traces._is_logfire_configured("https://logfire.pydantic.dev/") is False

    def test_user_project_url_is_configured_detection(self) -> None:
        """사용자 프로젝트 trace URL → 연동."""
        traces = _import_traces_module()
        assert traces._is_logfire_configured(
            "https://logfire-eu.pydantic.dev/myorg/myproject"
        ) is True

    def test_user_url_with_trailing_slash_is_configured_detection(self) -> None:
        """사용자 URL + trailing slash → 연동."""
        traces = _import_traces_module()
        assert traces._is_logfire_configured(
            "https://logfire-eu.pydantic.dev/myorg/myproject/"
        ) is True

    def test_whitespace_padding_handled_detection(self) -> None:
        """공백 패딩된 기본값 → 미연동 (strip 동작)."""
        traces = _import_traces_module()
        assert traces._is_logfire_configured("  https://logfire.pydantic.dev  ") is False


class TestModuleLevelConstant:
    """Module-level `_LOGFIRE_CONFIGURED` 상수가 env var 반영하는지 검증."""

    def test_unset_env_yields_default_url_and_not_configured_detection(self) -> None:
        """LOGFIRE_URL 미설정 → 기본값 사용 → _LOGFIRE_CONFIGURED = False."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOGFIRE_URL", None)
            traces = _import_traces_module()
        assert traces._LOGFIRE_URL == "https://logfire.pydantic.dev"
        assert traces._LOGFIRE_CONFIGURED is False

    def test_user_env_yields_configured_state_detection(self) -> None:
        """LOGFIRE_URL=사용자 URL → _LOGFIRE_CONFIGURED = True."""
        with patch.dict(
            os.environ,
            {"LOGFIRE_URL": "https://logfire-eu.pydantic.dev/my/proj"},
            clear=False,
        ):
            traces = _import_traces_module()
        assert traces._LOGFIRE_CONFIGURED is True
