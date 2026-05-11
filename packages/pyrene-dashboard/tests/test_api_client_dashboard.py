"""Unit tests for pyrene_dashboard.api_client module.

Uses ``unittest.mock.patch`` on ``get_client`` to inject a mock httpx.Client
and avoid real HTTP calls.  Naming suffix ``_dashboard`` per PLAN-016.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_mock_response(json_data: object, status_code: int = 200) -> MagicMock:
    """Return a mock httpx.Response-like object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# fetch_rbac_matrix
# ---------------------------------------------------------------------------


class TestFetchRbacMatrix:
    def test_returns_matrix_dict_dashboard(self) -> None:
        mock_data = {
            "roles": [
                {"role_id": "r1", "role_name": "admin", "tools": {"query": "allow"}},
            ],
            "tools": ["query"],
        }
        mock_client = MagicMock()
        mock_client.get.return_value = _make_mock_response(mock_data)

        with (
            patch("pyrene_dashboard.api_client.get_client", return_value=mock_client),
            patch("streamlit.cache_data", lambda **kw: (lambda f: f)),
        ):
            from pyrene_dashboard.api_client import fetch_rbac_matrix

            fn = fetch_rbac_matrix
            result = fn.__wrapped__("tok") if hasattr(fn, "__wrapped__") else fn("tok")

        assert isinstance(result, dict)

    def test_raises_on_http_error_dashboard(self) -> None:
        mock_client = MagicMock()
        resp = _make_mock_response({}, 401)
        resp.raise_for_status.side_effect = Exception("401 Unauthorized")
        mock_client.get.return_value = resp

        with patch("pyrene_dashboard.api_client.get_client", return_value=mock_client):
            from pyrene_dashboard.api_client import fetch_rbac_matrix

            with pytest.raises(Exception, match="401"):
                fetch_rbac_matrix("bad_token")


# ---------------------------------------------------------------------------
# fetch_denials_last_hour
# ---------------------------------------------------------------------------


class TestFetchDenialsLastHour:
    def test_counts_deny_outcomes_dashboard(self) -> None:
        audit_items = [
            {"outcome": "deny", "event_type": "rbac_check", "created_at": "2026-05-11T10:00:00Z"},
            {"outcome": "deny", "event_type": "rbac_check", "created_at": "2026-05-11T10:01:00Z"},
            {"outcome": "allow", "event_type": "rbac_check", "created_at": "2026-05-11T10:02:00Z"},
        ]
        mock_audit_response = {"items": audit_items, "total": 3, "page": 1, "size": 100}

        with patch(
            "pyrene_dashboard.api_client.fetch_audit_events",
            return_value=mock_audit_response,
        ):
            from pyrene_dashboard.api_client import fetch_denials_last_hour

            fn = fetch_denials_last_hour
            result = fn.__wrapped__("tok") if hasattr(fn, "__wrapped__") else fn("tok")

        assert result["count"] == 2
        assert len(result["recent"]) == 2

    def test_returns_zero_when_no_denials_dashboard(self) -> None:
        mock_audit_response = {
            "items": [
                {
                    "outcome": "allow",
                    "event_type": "rbac_check",
                    "created_at": "2026-05-11T10:00:00Z",
                }
            ],
            "total": 1,
            "page": 1,
            "size": 100,
        }
        with patch(
            "pyrene_dashboard.api_client.fetch_audit_events",
            return_value=mock_audit_response,
        ):
            from pyrene_dashboard.api_client import fetch_denials_last_hour

            fn = fetch_denials_last_hour
            result = fn.__wrapped__("tok") if hasattr(fn, "__wrapped__") else fn("tok")

        assert result["count"] == 0
        assert result["recent"] == []


# ---------------------------------------------------------------------------
# fetch_budget_blocked
# ---------------------------------------------------------------------------


class TestFetchBudgetBlocked:
    def test_returns_count_and_trend_dashboard(self) -> None:
        blocked_items = [
            {
                "outcome": "budget_exceeded",
                "event_type": "budget_exceeded",
                "created_at": "2026-05-11T09:00:00Z",
            },
            {
                "outcome": "budget_exceeded",
                "event_type": "budget_exceeded",
                "created_at": "2026-05-10T09:00:00Z",
            },
        ]
        mock_response = {"items": blocked_items, "total": 2, "page": 1, "size": 100}

        with patch(
            "pyrene_dashboard.api_client.fetch_audit_events", return_value=mock_response
        ):
            from pyrene_dashboard.api_client import fetch_budget_blocked

            fn = fetch_budget_blocked
            result = fn.__wrapped__("tok") if hasattr(fn, "__wrapped__") else fn("tok")

        assert result["count"] == 2
        # Should have 2 distinct days in trend
        assert len(result["trend"]) == 2

    def test_returns_zero_on_api_failure_dashboard(self) -> None:
        with patch(
            "pyrene_dashboard.api_client.fetch_audit_events",
            side_effect=Exception("network error"),
        ):
            from pyrene_dashboard.api_client import fetch_budget_blocked

            fn = fetch_budget_blocked
            result = fn.__wrapped__("tok") if hasattr(fn, "__wrapped__") else fn("tok")

        assert result["count"] == 0
        assert result["trend"] == []


# ---------------------------------------------------------------------------
# fetch_usage_records — server-side paging params forwarded
# ---------------------------------------------------------------------------


class TestFetchUsageRecords:
    def test_passes_paging_params_to_api_dashboard(self) -> None:
        mock_page = {
            "items": [],
            "page": 3,
            "size": 50,
            "total": 200,
        }
        mock_client = MagicMock()
        mock_client.get.return_value = _make_mock_response(mock_page)

        with patch("pyrene_dashboard.api_client.get_client", return_value=mock_client):
            from pyrene_dashboard.api_client import fetch_usage_records

            fetch_usage_records("tok", page=3, size=50, order_by="cost_usd")

        call_kwargs = mock_client.get.call_args
        sent_params: dict[str, object] = call_kwargs[1]["params"]
        assert sent_params["page"] == 3
        assert sent_params["size"] == 50
        assert sent_params["order_by"] == "cost_usd"


# ---------------------------------------------------------------------------
# fetch_usage_summary
# ---------------------------------------------------------------------------


class TestFetchUsageSummary:
    def test_returns_list_dashboard(self) -> None:
        summary_data = [
            {
                "period": "day",
                "period_label": "2026-05-11",
                "total_input_tokens": 100,
                "total_output_tokens": 50,
                "total_cache_read_tokens": 0,
                "total_cache_write_tokens": 0,
                "total_cost_usd": "0.00150000",
                "request_count": 3,
                "avg_attempts": "1.00",
            }
        ]
        mock_client = MagicMock()
        mock_client.get.return_value = _make_mock_response(summary_data)

        with patch("pyrene_dashboard.api_client.get_client", return_value=mock_client):
            from pyrene_dashboard.api_client import fetch_usage_summary

            result = fetch_usage_summary("tok", period="day")

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["period_label"] == "2026-05-11"
