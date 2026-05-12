"""PRD-032 회귀 가드: fetch_or_stale helper + format_age_korean.

3 분기 (성공 / stale / no-cache) + 시간 포맷 검증. PRD-026 의 retry button
textual unit 은 본 PR 에서 *helper 흡수* 로 마이그레이션 — 11 호출지점이
모두 `fetch_or_stale` 사용 인지 textual scan 으로 검증 (대체 가드).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pyrene_dashboard.api_client import fetch_or_stale, format_age_korean

_PAGES_DIR = Path(__file__).parent.parent / "src" / "pyrene_dashboard" / "pages"


# ---------------------------------------------------------------------------
# format_age_korean
# ---------------------------------------------------------------------------


class TestFormatAgeKorean:
    def test_none_returns_now_stale(self) -> None:
        assert format_age_korean(None) == "방금"

    def test_sub_second_returns_now_stale(self) -> None:
        assert format_age_korean(time.time()) == "방금"

    def test_30_seconds_stale(self) -> None:
        ts = time.time() - 30
        result = format_age_korean(ts)
        assert result.endswith("초 전"), f"기대 'N초 전' 끝남, got: {result}"
        assert "30" in result or "29" in result

    def test_5_minutes_stale(self) -> None:
        ts = time.time() - 300
        result = format_age_korean(ts)
        assert result.endswith("분 전"), f"기대 'N분 전' 끝남, got: {result}"
        assert "5" in result

    def test_2_hours_stale(self) -> None:
        ts = time.time() - 7200
        result = format_age_korean(ts)
        assert result.endswith("시간 전"), f"기대 'N시간 전' 끝남, got: {result}"
        assert "2" in result


# ---------------------------------------------------------------------------
# fetch_or_stale 3 분기
# ---------------------------------------------------------------------------


class TestFetchOrStale:
    def test_success_caches_in_session_state_stale(self) -> None:
        """성공 분기: fetcher 호출, session_state 갱신, 데이터 반환."""
        mock_session_state: dict[str, Any] = {}
        mock_fetcher = MagicMock(return_value={"ok": True})

        with (
            patch("pyrene_dashboard.api_client.st.session_state", mock_session_state),
            patch("pyrene_dashboard.api_client.st.spinner") as mock_spinner,
        ):
            mock_spinner.return_value.__enter__ = MagicMock(return_value=None)
            mock_spinner.return_value.__exit__ = MagicMock(return_value=False)
            result = fetch_or_stale(
                key="test", context="테스트", fetcher=mock_fetcher
            )

        assert result == {"ok": True}
        assert "_stale_test" in mock_session_state
        cached_data, cached_ts = mock_session_state["_stale_test"]
        assert cached_data == {"ok": True}
        assert isinstance(cached_ts, float)
        mock_fetcher.assert_called_once_with()

    def test_failure_with_cache_returns_stale_with_warning(self) -> None:
        """실패 + cache 있음: st.warning + stale 데이터 반환."""
        mock_session_state: dict[str, Any] = {
            "_stale_test": ({"cached": "data"}, time.time() - 60)
        }
        mock_fetcher = MagicMock(side_effect=RuntimeError("네트워크 실패"))

        with (
            patch("pyrene_dashboard.api_client.st.session_state", mock_session_state),
            patch("pyrene_dashboard.api_client.st.spinner") as mock_spinner,
            patch("pyrene_dashboard.api_client.st.warning") as mock_warning,
            patch("pyrene_dashboard.api_client.st.error") as mock_error,
        ):
            mock_spinner.return_value.__enter__ = MagicMock(return_value=None)
            mock_spinner.return_value.__exit__ = MagicMock(return_value=False)
            result = fetch_or_stale(
                key="test", context="테스트", fetcher=mock_fetcher
            )

        assert result == {"cached": "data"}
        mock_warning.assert_called_once()
        warning_msg = mock_warning.call_args.args[0]
        assert "⚠️" in warning_msg
        assert "마지막 갱신" in warning_msg
        mock_error.assert_not_called()

    def test_failure_without_cache_returns_none_with_error(self) -> None:
        """실패 + cache 없음: st.error + retry button + None 반환."""
        mock_session_state: dict[str, Any] = {}
        mock_fetcher = MagicMock(side_effect=RuntimeError("네트워크 실패"))
        mock_fetcher.clear = MagicMock()

        with (
            patch("pyrene_dashboard.api_client.st.session_state", mock_session_state),
            patch("pyrene_dashboard.api_client.st.spinner") as mock_spinner,
            patch("pyrene_dashboard.api_client.st.warning") as mock_warning,
            patch("pyrene_dashboard.api_client.st.error") as mock_error,
            patch("pyrene_dashboard.api_client.st.button", return_value=False) as mock_button,
        ):
            mock_spinner.return_value.__enter__ = MagicMock(return_value=None)
            mock_spinner.return_value.__exit__ = MagicMock(return_value=False)
            result = fetch_or_stale(
                key="test", context="테스트", fetcher=mock_fetcher
            )

        assert result is None
        mock_warning.assert_not_called()
        mock_error.assert_called_once()
        mock_button.assert_called_once()
        button_kwargs = mock_button.call_args.kwargs
        assert button_kwargs.get("key") == "retry_test"


# ---------------------------------------------------------------------------
# 11 호출지점 매트릭스 (PRD-026 textual unit 마이그레이션 — helper 흡수 후)
# ---------------------------------------------------------------------------


class TestFetchOrStaleCallSiteMatrix:
    """11 fetch 호출지점 모두 fetch_or_stale 사용 검증.

    PRD-026 의 retry button textual unit (스캔 패턴) 의 *대체 가드* —
    helper 흡수 후 11 호출지점이 모두 동일 helper 호출인지 정적 검증.
    """

    def test_eleven_call_sites_use_fetch_or_stale_dashboard(self) -> None:
        call_count = 0
        for page in _PAGES_DIR.glob("*.py"):
            if page.name == "__init__.py":
                continue
            content = page.read_text(encoding="utf-8")
            # fetch_or_stale( 호출 (import 제외)
            call_count += len(re.findall(r"\bfetch_or_stale\s*\(", content))
        assert call_count == 11, f"11 호출 기대, {call_count} 발견"

    def test_no_legacy_try_spinner_blocks_in_pages_dashboard(self) -> None:
        """기존 try/with st.spinner 패턴이 pages 에 잔존하지 않음 (helper 흡수)."""
        legacy_spinner_count = 0
        for page in _PAGES_DIR.glob("*.py"):
            if page.name == "__init__.py":
                continue
            content = page.read_text(encoding="utf-8")
            legacy_spinner_count += content.count('with st.spinner("최신 데이터 동기화 중')
        assert legacy_spinner_count == 0, (
            f"legacy spinner 블록 {legacy_spinner_count}건 잔존 — fetch_or_stale 마이그레이션 미완"
        )

    def test_no_legacy_retry_button_in_pages_dashboard(self) -> None:
        """기존 retry button 패턴이 pages 에 잔존하지 않음 (helper 안으로 흡수)."""
        retry_count = 0
        for page in _PAGES_DIR.glob("*.py"):
            if page.name == "__init__.py":
                continue
            retry_count += page.read_text(encoding="utf-8").count("🔄 재시도")
        assert retry_count == 0, (
            f"legacy retry button {retry_count}건 잔존 — helper 흡수 미완"
        )

    @pytest.mark.parametrize(
        "expected_key",
        [
            "overview_rbac",
            "overview_denials",
            "overview_budget",
            "overview_cost",
            "overview_users",
            "usage_records",
            "audit_timeline",
            "audit_events",
            "traces_events",
            "rbac_matrix",
            "data_permissions",
        ],
    )
    def test_each_key_appears_in_pages_dashboard(self, expected_key: str) -> None:
        """11 key 가 모두 *어딘가의 페이지* 에 등장 (호출지점 매트릭스 보존)."""
        found = False
        for page in _PAGES_DIR.glob("*.py"):
            if page.name == "__init__.py":
                continue
            content = page.read_text(encoding="utf-8")
            if f'key="{expected_key}"' in content:
                found = True
                break
        assert found, f"key=\"{expected_key}\" 가 pages 어디에도 없음"
