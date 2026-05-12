"""PRD-033 / PRD-019 F-3: bridge_sql_settings_to_environ() 회귀 가드.

3 분기 검증:
- Settings 인스턴스화 성공 + anthropic_api_key 있음 → os.environ 에 setdefault
- Settings 인스턴스화 성공 + anthropic_api_key 없음 → os.environ 변경 없음
- Settings 인스턴스화 실패 (PG_DSN 미설정 등) → 조용히 fallback, raise 없음
+ setdefault invariant — shell-exported 값 우선권 보존 (PRD-019 F-3 핵심).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from pyrene_sql.env_bridge import bridge_sql_settings_to_environ


class TestBridgeSqlSettingsToEnviron:
    """`bridge_sql_settings_to_environ()` 3 분기."""

    def test_settings_with_anthropic_key_sets_environ_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settings 성공 + anthropic_api_key 있음 → os.environ 에 set."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_settings = MagicMock()
        mock_settings.anthropic_api_key = "sk-test-key-12345"

        with patch(
            "pyrene_sql.settings.Settings", return_value=mock_settings
        ):
            bridge_sql_settings_to_environ()

        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-test-key-12345"

    def test_settings_load_failure_silently_returns_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settings() 가 raise (예: PG_DSN 미설정) → 조용히 fallback, no-op."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        original_environ = dict(os.environ)

        with patch(
            "pyrene_sql.settings.Settings",
            side_effect=RuntimeError("PG_DSN missing"),
        ):
            bridge_sql_settings_to_environ()  # raise 안 함

        # os.environ 에 ANTHROPIC_API_KEY 추가 없음
        assert os.environ.get("ANTHROPIC_API_KEY") is None
        # 다른 환경변수 영향 없음
        for k, v in original_environ.items():
            if k != "ANTHROPIC_API_KEY":
                assert os.environ.get(k) == v

    def test_settings_with_none_anthropic_key_no_op_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settings 성공 + anthropic_api_key 가 None → os.environ 변경 없음."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_settings = MagicMock()
        mock_settings.anthropic_api_key = None

        with patch(
            "pyrene_sql.settings.Settings", return_value=mock_settings
        ):
            bridge_sql_settings_to_environ()

        assert os.environ.get("ANTHROPIC_API_KEY") is None

    def test_setdefault_preserves_shell_exported_value_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PRD-019 F-3 invariant: shell-exported ANTHROPIC_API_KEY 우선권 보존.

        사용자가 이미 shell-export 한 값이 있으면 `setdefault` 가 *덮어쓰지 않음* —
        Settings 의 값보다 shell-export 우선 (디버깅 친화).
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-shell-exported")
        mock_settings = MagicMock()
        mock_settings.anthropic_api_key = "sk-from-env-file"

        with patch(
            "pyrene_sql.settings.Settings", return_value=mock_settings
        ):
            bridge_sql_settings_to_environ()

        # shell-export 값이 그대로 — Settings 값으로 덮어쓰지 않음
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-shell-exported"

    def test_empty_string_anthropic_key_no_op_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """anthropic_api_key 가 빈 문자열 → falsy → no-op."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_settings = MagicMock()
        mock_settings.anthropic_api_key = ""

        with patch(
            "pyrene_sql.settings.Settings", return_value=mock_settings
        ):
            bridge_sql_settings_to_environ()

        assert os.environ.get("ANTHROPIC_API_KEY") is None
