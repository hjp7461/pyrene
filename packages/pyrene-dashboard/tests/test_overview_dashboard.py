"""Unit tests for pages/overview.py — Page 1 (RBAC heatmap redesign).

Tests the helper functions in overview.py directly:
  - _build_heatmap_df: 4x4 matrix slicing + DataFrame shape
  - _color_cell: correct CSS per action value
  - Integration: mock API → verify counter values > 0

AppTest is not used here because st.fragment with run_every requires
a running Streamlit server. The helper functions are tested directly.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers: import the private helpers from overview.py directly
# ---------------------------------------------------------------------------


def _make_col_mock() -> MagicMock:
    """Return a MagicMock that supports the context manager protocol."""
    col = MagicMock()
    col.__enter__ = MagicMock(return_value=col)
    col.__exit__ = MagicMock(return_value=False)
    return col


def _make_columns_side_effect(*args: object, **kwargs: object) -> list[MagicMock]:
    """Return the right number of column mocks based on the argument to st.columns."""
    spec = args[0] if args else 1
    if isinstance(spec, (list, tuple)):
        n = len(spec)
    elif isinstance(spec, int):
        n = spec
    else:
        n = 1
    return [_make_col_mock() for _ in range(n)]


def _import_helpers() -> tuple:  # type: ignore[type-arg]
    """Import _build_heatmap_df and _color_cell from overview.py.

    We patch Streamlit calls and auth so the module-level code does not
    execute during import.
    """
    # Remove any cached module so patches take effect cleanly
    for mod_key in list(sys.modules.keys()):
        if "pyrene_dashboard.pages.overview" in mod_key:
            del sys.modules[mod_key]

    fake_token = "admin_jwt"
    state = {
        "auth_token": fake_token,
        "auth_role": ["admin"],
        "auth_expires_at": 9999999999.0,
    }

    with (
        patch("streamlit.session_state", state),
        patch("streamlit.title"),
        patch("streamlit.subheader"),
        patch("streamlit.columns", side_effect=_make_columns_side_effect),
        patch("streamlit.metric"),
        patch("streamlit.dataframe"),
        patch("streamlit.line_chart"),
        patch("streamlit.divider"),
        patch("streamlit.caption"),
        patch("streamlit.info"),
        patch("streamlit.error"),
        patch("streamlit.success"),
        patch("streamlit.fragment", lambda **kw: (lambda f: f)),
        patch("pyrene_dashboard.auth.require_admin", return_value=fake_token),
        patch(
            "pyrene_dashboard.api_client.fetch_rbac_matrix",
            return_value={"roles": [], "tools": []},
        ),
        patch(
            "pyrene_dashboard.api_client.fetch_denials_last_hour",
            return_value={"count": 0, "recent": []},
        ),
        patch(
            "pyrene_dashboard.api_client.fetch_budget_blocked",
            return_value={"count": 0, "trend": []},
        ),
        patch("pyrene_dashboard.api_client.fetch_usage_summary", return_value=[]),
    ):
        import pyrene_dashboard.pages.overview as ov

        return ov._build_heatmap_df, ov._color_cell


# ---------------------------------------------------------------------------
# Test _build_heatmap_df
# ---------------------------------------------------------------------------


class TestBuildHeatmapDf:
    def setup_method(self) -> None:
        self.build_heatmap_df, self.color_cell = _import_helpers()

    def test_empty_matrix_returns_empty_df_dashboard(self) -> None:
        df = self.build_heatmap_df({"roles": [], "tools": []})
        assert df.empty

    def test_4x4_matrix_correct_shape_dashboard(self) -> None:
        matrix = {
            "roles": [
                {"role_name": "admin", "tools": {
                    "query": "allow", "insert": "deny", "delete": "deny", "update": "allow",
                }},
                {"role_name": "viewer", "tools": {
                    "query": "allow", "insert": "deny", "delete": "deny", "update": "deny",
                }},
                {"role_name": "editor", "tools": {
                    "query": "allow", "insert": "allow", "delete": "deny", "update": "allow",
                }},
                {"role_name": "auditor", "tools": {
                    "query": "allow", "insert": "deny", "delete": "deny", "update": "deny",
                }},
            ],
            "tools": ["query", "insert", "delete", "update"],
        }
        df = self.build_heatmap_df(matrix)
        assert df.shape == (4, 4), f"Expected 4x4 but got {df.shape}"
        assert list(df.columns) == ["query", "insert", "delete", "update"]
        assert list(df.index) == ["admin", "viewer", "editor", "auditor"]

    def test_clamps_to_4_roles_and_tools_dashboard(self) -> None:
        """Matrix larger than 4x4 is clamped to first 4 roles and 4 tools."""
        roles = [
            {"role_name": f"role{i}", "tools": {f"tool{j}": "allow" for j in range(6)}}
            for i in range(6)
        ]
        matrix = {"roles": roles, "tools": [f"tool{j}" for j in range(6)]}
        df = self.build_heatmap_df(matrix)
        assert df.shape[0] <= 4
        assert df.shape[1] <= 4

    def test_missing_tool_defaults_to_deny_dashboard(self) -> None:
        matrix = {
            "roles": [{"role_name": "viewer", "tools": {}}],
            "tools": ["query"],
        }
        df = self.build_heatmap_df(matrix)
        assert df.loc["viewer", "query"] == "deny"


# ---------------------------------------------------------------------------
# Test _color_cell
# ---------------------------------------------------------------------------


class TestColorCell:
    def setup_method(self) -> None:
        _, self.color_cell = _import_helpers()

    def test_allow_returns_green_dashboard(self) -> None:
        style = self.color_cell("allow")
        assert "#22c55e" in style

    def test_deny_returns_red_dashboard(self) -> None:
        style = self.color_cell("deny")
        assert "#ef4444" in style

    def test_unknown_returns_amber_dashboard(self) -> None:
        style = self.color_cell("warning")
        assert "#f59e0b" in style

    def test_allow_is_case_insensitive_dashboard(self) -> None:
        assert "#22c55e" in self.color_cell("ALLOW")
        assert "#ef4444" in self.color_cell("DENY")


# ---------------------------------------------------------------------------
# Integration: mock API values reflected in denial counter
# ---------------------------------------------------------------------------


class TestOverviewWithMockData:
    def test_denial_count_positive_when_api_returns_denials_dashboard(self) -> None:
        """Denial counter is correctly derived from mock API data."""
        audit_items = [
            {
                "outcome": "deny",
                "event_type": "rbac_check",
                "created_at": "2026-05-11T10:00:00Z",
                "user_id": "u1",
                "tool_name": "query",
            },
            {
                "outcome": "deny",
                "event_type": "rbac_check",
                "created_at": "2026-05-11T10:01:00Z",
                "user_id": "u2",
                "tool_name": "insert",
            },
        ]
        mock_audit = {"items": audit_items, "total": 2, "page": 1, "size": 100}

        with patch("pyrene_dashboard.api_client.fetch_audit_events", return_value=mock_audit):
            from pyrene_dashboard.api_client import fetch_denials_last_hour

            fn = fetch_denials_last_hour
            result = fn.__wrapped__("tok") if hasattr(fn, "__wrapped__") else fn("tok")

        assert result["count"] == 2

    def test_heatmap_all_cells_have_valid_action_dashboard(self) -> None:
        """All heatmap cells must contain 'allow' or 'deny'."""
        matrix = {
            "roles": [
                {"role_name": "admin", "tools": {"query": "allow", "insert": "deny"}},
                {"role_name": "viewer", "tools": {"query": "allow"}},
            ],
            "tools": ["query", "insert"],
        }
        build_heatmap_df, _color_cell = _import_helpers()
        df = build_heatmap_df(matrix)
        valid_actions = {"allow", "deny"}
        for val in df.values.flatten():
            assert val in valid_actions, f"Invalid action: {val}"

    def test_budget_blocked_zero_on_empty_response_dashboard(self) -> None:
        """Budget blocked returns 0 when no blocked events returned."""
        empty_response = {"items": [], "total": 0, "page": 1, "size": 100}
        with patch("pyrene_dashboard.api_client.fetch_audit_events", return_value=empty_response):
            from pyrene_dashboard.api_client import fetch_budget_blocked

            fn = fetch_budget_blocked
            result = fn.__wrapped__("tok") if hasattr(fn, "__wrapped__") else fn("tok")

        assert result["count"] == 0
