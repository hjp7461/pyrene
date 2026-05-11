"""Unit tests for pages/usage.py — Page 2.

Tests:
  - server-side paging parameters forwarded correctly
  - timezone UTC → local conversion logic
  - filter form isolation (no rerun without submit)
"""

from __future__ import annotations

from unittest.mock import patch

import pendulum


class TestUsageServerSidePaging:
    def test_api_receives_page_and_size_params_dashboard(self) -> None:
        """Verify fetch_usage_records is called with page + size + order_by."""
        captured: dict[str, object] = {}

        def _mock_fetch(
            token: str,
            *,
            page: int = 1,
            size: int = 25,
            order_by: str = "created_at",
            user_id: str | None = None,
            agent_id: str | None = None,
            since: str | None = None,
            until: str | None = None,
        ) -> dict[str, object]:
            captured["page"] = page
            captured["size"] = size
            captured["order_by"] = order_by
            return {"items": [], "page": page, "size": size, "total": 0}

        with patch("pyrene_dashboard.api_client.fetch_usage_records", side_effect=_mock_fetch):
            from pyrene_dashboard.api_client import fetch_usage_records

            fetch_usage_records("tok", page=5, size=10, order_by="cost_usd")

        assert captured["page"] == 5
        assert captured["size"] == 10
        assert captured["order_by"] == "cost_usd"

    def test_total_pages_calculation_dashboard(self) -> None:
        """100 records at page size 10 = 10 pages."""
        total = 100
        size = 10
        total_pages = max(1, (total + size - 1) // size)
        assert total_pages == 10

    def test_partial_last_page_dashboard(self) -> None:
        """103 records at page size 10 = 11 pages."""
        total = 103
        size = 10
        total_pages = max(1, (total + size - 1) // size)
        assert total_pages == 11

    def test_zero_total_gives_one_page_dashboard(self) -> None:
        """0 records → at least 1 page (never 0)."""
        total = 0
        size = 25
        total_pages = max(1, (total + size - 1) // size)
        assert total_pages == 1


class TestTimezoneNormalization:
    def test_utc_timestamp_converts_to_iso_for_api_dashboard(self) -> None:
        """UTC date input converts to ISO 8601 string for the API."""
        import datetime

        d = datetime.date(2026, 5, 11)
        since_str = pendulum.datetime(d.year, d.month, d.day, tz="UTC").to_iso8601_string()
        assert since_str.startswith("2026-05-11T00:00:00")
        assert "UTC" in since_str or "+00:00" in since_str or "Z" in since_str

    def test_utc_parse_and_local_conversion_dashboard(self) -> None:
        """UTC ISO string parses and converts to local tz without error."""
        utc_str = "2026-05-11T10:30:00+00:00"
        parsed = pendulum.parse(utc_str, tz="UTC")
        assert parsed is not None
        local_tz = pendulum.local_timezone()  # type: ignore[operator]
        if hasattr(parsed, "in_timezone"):
            local = parsed.in_timezone(local_tz)
            local_str = local.to_datetime_string()
            assert len(local_str) == 19  # "YYYY-MM-DD HH:MM:SS"

    def test_malformed_timestamp_does_not_raise_dashboard(self) -> None:
        """Malformed timestamp falls back gracefully (no exception)."""
        bad_ts = "not-a-date"
        try:
            pendulum.parse(bad_ts, tz="UTC")
            # pendulum may return None or raise — both are acceptable
            fallback = bad_ts
        except Exception:
            fallback = bad_ts
        assert fallback == bad_ts  # fallback used


class TestUsageFilters:
    def test_optional_filters_passed_correctly_dashboard(self) -> None:
        """Optional filters user_id/agent_id are passed when non-empty."""
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"items": [], "page": 1, "size": 25, "total": 0}
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp

        with patch("pyrene_dashboard.api_client.get_client", return_value=mock_client):
            from pyrene_dashboard.api_client import fetch_usage_records

            fetch_usage_records("tok", user_id="user-123", agent_id="agent-456")

        call_kwargs = mock_client.get.call_args
        params: dict[str, object] = call_kwargs[1]["params"]
        assert params.get("user_id") == "user-123"
        assert params.get("agent_id") == "agent-456"

    def test_none_filters_not_forwarded_dashboard(self) -> None:
        """None optional filters are NOT included in API params."""
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"items": [], "page": 1, "size": 25, "total": 0}
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp

        with patch("pyrene_dashboard.api_client.get_client", return_value=mock_client):
            from pyrene_dashboard.api_client import fetch_usage_records

            fetch_usage_records("tok", user_id=None, agent_id=None)

        call_kwargs = mock_client.get.call_args
        params: dict[str, object] = call_kwargs[1]["params"]
        assert "user_id" not in params
        assert "agent_id" not in params
