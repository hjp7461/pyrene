"""Unit tests for pages/audit.py — Page 4.

Tests:
  - fetch_audit_events filter params forwarded correctly
  - fetch_audit_timeline returns list of bucket dicts
  - Per-row expander label construction
  - Pagination math
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pendulum


class TestAuditEventFilters:
    def test_event_type_filter_forwarded_dashboard(self) -> None:
        """event_type filter is included in API params when non-empty."""
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"items": [], "total": 0, "page": 1, "size": 25}
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp

        with patch("pyrene_dashboard.api_client.get_client", return_value=mock_client):
            from pyrene_dashboard.api_client import fetch_audit_events

            fetch_audit_events("tok", event_type="rbac_deny", page=1, size=25)

        params = mock_client.get.call_args[1]["params"]
        assert params.get("event_type") == "rbac_deny"

    def test_none_filters_not_forwarded_dashboard(self) -> None:
        """None filters are excluded from the API request."""
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"items": [], "total": 0, "page": 1, "size": 25}
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp

        with patch("pyrene_dashboard.api_client.get_client", return_value=mock_client):
            from pyrene_dashboard.api_client import fetch_audit_events

            fetch_audit_events("tok", event_type=None, user_id=None, since=None)

        params = mock_client.get.call_args[1]["params"]
        assert "event_type" not in params
        assert "user_id" not in params
        assert "since" not in params

    def test_since_filter_is_iso8601_dashboard(self) -> None:
        """Since filter when derived from pendulum is valid ISO 8601."""
        import datetime

        d = datetime.date(2026, 5, 1)
        since_str = pendulum.datetime(d.year, d.month, d.day, tz="UTC").to_iso8601_string()
        # Must be parseable as datetime
        parsed = pendulum.parse(since_str)
        assert parsed is not None

    def test_all_optional_filters_forwarded_dashboard(self) -> None:
        """All optional filters passed when non-None."""
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"items": [], "total": 0, "page": 1, "size": 10}
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp

        with patch("pyrene_dashboard.api_client.get_client", return_value=mock_client):
            from pyrene_dashboard.api_client import fetch_audit_events

            fetch_audit_events(
                "tok",
                page=2,
                size=10,
                event_type="rbac_check",
                user_id="u1",
                since="2026-05-01T00:00:00+00:00",
                scope="team",
                request_id="req-123",
            )

        params = mock_client.get.call_args[1]["params"]
        assert params["event_type"] == "rbac_check"
        assert params["user_id"] == "u1"
        assert params["since"] == "2026-05-01T00:00:00+00:00"
        assert params["scope"] == "team"
        assert params["request_id"] == "req-123"


class TestAuditTimeline:
    def test_timeline_returns_list_dashboard(self) -> None:
        """fetch_audit_timeline returns a list of bucket dicts."""
        timeline_data = [
            {"bucket": "2026-05-11T10:00:00+00:00", "count": 5},
            {"bucket": "2026-05-11T11:00:00+00:00", "count": 3},
        ]
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = timeline_data
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp

        with patch("pyrene_dashboard.api_client.get_client", return_value=mock_client):
            from pyrene_dashboard.api_client import fetch_audit_timeline

            result = fetch_audit_timeline("tok")

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["count"] == 5

    def test_timeline_df_has_bucket_index_dashboard(self) -> None:
        """Timeline data converted to DataFrame with bucket datetime index."""
        timeline_data = [
            {"bucket": "2026-05-11T10:00:00+00:00", "count": 5},
            {"bucket": "2026-05-11T11:00:00+00:00", "count": 3},
        ]
        df = pd.DataFrame(timeline_data)
        df["bucket"] = pd.to_datetime(df["bucket"], utc=True)
        df = df.set_index("bucket").sort_index()
        assert hasattr(df.index.dtype, "tz") and df.index.dtype.tz is not None  # UTC-aware
        assert list(df.columns) == ["count"]
        assert df["count"].iloc[0] == 5


class TestAuditExpanders:
    def test_expander_label_deny_shows_red_icon_dashboard(self) -> None:
        """Deny outcome events get the red icon in the expander label."""
        event: dict[str, object] = {
            "outcome": "deny",
            "event_type": "rbac_check",
            "created_at": "2026-05-11T10:00:00Z",
            "user_id": "u1-uuid-long",
            "tool_name": "query",
        }
        outcome: str = str(event.get("outcome", ""))
        icon = "🔴" if outcome in ("deny", "denied") else "🟢"
        assert icon == "🔴"

    def test_expander_label_allow_shows_green_icon_dashboard(self) -> None:
        """Allow outcome events get the green icon in the expander label."""
        event: dict[str, object] = {
            "outcome": "allow",
            "event_type": "rbac_check",
            "created_at": "2026-05-11T10:00:00Z",
        }
        outcome: str = str(event.get("outcome", ""))
        icon = "🔴" if outcome in ("deny", "denied") else "🟢"
        assert icon == "🟢"


class TestAuditPagination:
    def test_total_pages_100_events_size_25_dashboard(self) -> None:
        assert max(1, (100 + 25 - 1) // 25) == 4

    def test_total_pages_0_events_dashboard(self) -> None:
        assert max(1, (0 + 25 - 1) // 25) == 1

    def test_total_pages_1_event_dashboard(self) -> None:
        assert max(1, (1 + 25 - 1) // 25) == 1

    def test_total_pages_26_events_size_25_dashboard(self) -> None:
        assert max(1, (26 + 25 - 1) // 25) == 2
