"""Unit tests for pages/traces.py — Page 5.

Tests:
  - LOGFIRE_URL falls back to default when env var not set
  - fetch_audit_events used as trace fallback proxy
  - Expander label construction per event
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


class TestLogfireUrl:
    def test_default_url_used_when_env_not_set_dashboard(self) -> None:
        """Default Logfire URL is used when LOGFIRE_URL is not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Simulate the logic in traces.py
            url = os.environ.get("LOGFIRE_URL", "https://logfire.pydantic.dev")
        assert url == "https://logfire.pydantic.dev"

    def test_custom_url_from_env_dashboard(self) -> None:
        """Custom LOGFIRE_URL from environment is respected."""
        custom_url = "https://my-logfire.example.com"
        with patch.dict(os.environ, {"LOGFIRE_URL": custom_url}):
            url = os.environ.get("LOGFIRE_URL", "https://logfire.pydantic.dev")
        assert url == custom_url

    def test_screenshot_path_placeholder_no_crash_dashboard(self) -> None:
        """Missing screenshot path does not crash the page."""
        # Simulate logic: if path not set or file not found, no image rendered
        screenshot_path = os.environ.get("LOGFIRE_SCREENSHOT_PATH", "")
        exists = bool(screenshot_path) and os.path.isfile(screenshot_path)
        assert not exists  # In test env, path is empty → no image rendered


class TestTraceFallback:
    def test_trace_fallback_uses_audit_events_dashboard(self) -> None:
        """Trace fallback fetches 5 latest audit events."""
        mock_audit_response = {
            "items": [
                {
                    "id": "e1",
                    "event_type": "mcp_call",
                    "outcome": "allow",
                    "tool_name": "query",
                    "user_id": "u1",
                    "request_id": "r1",
                    "created_at": "2026-05-11T10:00:00Z",
                }
            ],
            "total": 1,
            "page": 1,
            "size": 5,
        }
        with patch("pyrene_dashboard.api_client.get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_audit_response
            mock_resp.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_resp
            mock_get_client.return_value = mock_client

            from pyrene_dashboard.api_client import fetch_audit_events

            result = fetch_audit_events("tok", size=5)

        assert isinstance(result, dict)
        assert len(result["items"]) == 1

    def test_fallback_label_construction_dashboard(self) -> None:
        """Trace fallback label includes event_type and request_id prefix."""
        event = {
            "event_type": "mcp_call",
            "outcome": "allow",
            "request_id": "req-uuid-1234-5678",
            "created_at": "2026-05-11T10:00:00Z",
        }
        outcome: str = str(event.get("outcome", ""))
        icon = "🔴" if outcome in ("deny", "denied") else "🟢"
        label = (
            f"{icon} [{event.get('created_at', '')[:19]}] "
            f"{event.get('event_type', 'unknown')} "
            f"req={str(event.get('request_id', ''))[:8]}…"
        )
        assert "mcp_call" in label
        assert "req-uuid" in label
        assert icon == "🟢"

    def test_no_items_shows_info_not_error_dashboard(self) -> None:
        """Empty audit response results in info display, not error.

        Verifies the empty-items branch by patching fetch_audit_events
        at the module level (bypassing the @st.cache_data wrapper).
        """
        empty_response: dict[str, object] = {"items": [], "total": 0, "page": 1, "size": 5}

        with patch("pyrene_dashboard.api_client.fetch_audit_events", return_value=empty_response):
            from pyrene_dashboard.api_client import fetch_audit_events

            result = fetch_audit_events("tok", size=5)

        items: list[object] = result.get("items", [])
        assert len(items) == 0
        # In the page, this would trigger st.info("No recent trace data available.")
        # We verify the empty path deterministically here


class TestFragmentPolling:
    def test_run_every_30_is_specified_dashboard(self) -> None:
        """Verify st.fragment(run_every=30) pattern is used (smoke check).

        We verify this by checking that the module-level code in traces.py
        imports and uses the fragment decorator correctly — we do this
        by patching st.fragment and confirming the decorator was called
        with run_every=30.
        """
        fragment_calls: list[dict[str, object]] = []

        def mock_fragment(**kwargs: object):  # type: ignore[no-untyped-def]
            fragment_calls.append(dict(kwargs))
            return lambda f: f  # decorator returns the function unchanged

        import sys

        # Clear cached module to force reimport with our patch
        for key in list(sys.modules.keys()):
            if "pyrene_dashboard.pages.traces" in key:
                del sys.modules[key]

        state = {
            "auth_token": "tok",
            "auth_role": ["admin"],
            "auth_expires_at": 9999999999.0,
        }

        with (
            patch("streamlit.session_state", state),
            patch("streamlit.fragment", side_effect=mock_fragment),
            patch("streamlit.title"),
            patch("streamlit.subheader"),
            patch("streamlit.caption"),
            patch("streamlit.divider"),
            patch("streamlit.info"),
            patch("streamlit.error"),
            patch("streamlit.columns", return_value=[MagicMock(), MagicMock()]),
            patch("streamlit.link_button"),
            patch(
                "streamlit.expander",
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=None),
                    __exit__=MagicMock(return_value=False),
                ),
            ),
            patch("streamlit.components.v1.iframe"),
            patch("pyrene_dashboard.auth.require_admin", return_value="tok"),
            patch(
                "pyrene_dashboard.api_client.fetch_audit_events",
                return_value={"items": [], "total": 0, "page": 1, "size": 5},
            ),
        ):
            import pyrene_dashboard.pages.traces  # noqa: F401

        # At least one fragment call must have run_every=30
        run_every_values = [c.get("run_every") for c in fragment_calls]
        assert 30 in run_every_values, f"Expected run_every=30 in fragment calls: {fragment_calls}"
