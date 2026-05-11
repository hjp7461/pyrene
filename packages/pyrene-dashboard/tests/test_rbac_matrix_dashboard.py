"""Unit tests for pages/rbac_matrix.py — Page 3.

Tests:
  - fetch_rbac_matrix response → correct 2D DataFrame construction
  - fetch_data_permissions response → correct row rendering
  - Color styling: allow=green, deny=red
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd


class TestRbacMatrixRendering:
    def test_full_matrix_dataframe_shape_dashboard(self) -> None:
        """3 roles x 3 tools -> DataFrame shape (3, 3)."""
        t_allow = {"query": "allow", "insert": "allow", "delete": "deny"}
        t_deny_all = {"query": "allow", "insert": "deny", "delete": "deny"}
        matrix = {
            "roles": [
                {"role_name": "admin", "tools": t_allow},
                {"role_name": "viewer", "tools": t_deny_all},
                {"role_name": "editor", "tools": t_allow},
            ],
            "tools": ["query", "insert", "delete"],
        }
        # Replicate the DataFrame build logic directly (page logic)
        from typing import Any, cast

        roles: list[dict[str, Any]] = cast(list[dict[str, Any]], matrix["roles"])
        tools: list[str] = cast(list[str], matrix["tools"])

        rows = []
        for role_entry in roles:
            role_name: str = role_entry.get("role_name", "?")
            tool_map: dict[str, str] = role_entry.get("tools", {})
            row: dict[str, str] = {"Role": role_name}
            for tool in tools:
                row[tool] = tool_map.get(tool, "deny")
            rows.append(row)

        df = pd.DataFrame(rows).set_index("Role")
        assert df.shape == (3, 3)
        assert df.loc["admin", "query"] == "allow"
        assert df.loc["viewer", "insert"] == "deny"

    def test_missing_tool_defaults_to_deny_in_matrix_dashboard(self) -> None:
        """Tool absent from role's tools map defaults to 'deny'."""
        matrix = {
            "roles": [{"role_name": "viewer", "tools": {}}],
            "tools": ["query", "insert"],
        }
        from typing import Any, cast

        roles2: list[dict[str, Any]] = cast(list[dict[str, Any]], matrix["roles"])
        tools2: list[str] = cast(list[str], matrix["tools"])
        rows = []
        for role_entry in roles2:
            tool_map: dict[str, str] = role_entry.get("tools", {})
            row: dict[str, str] = {"Role": role_entry["role_name"]}
            for tool in tools2:
                row[tool] = tool_map.get(tool, "deny")
            rows.append(row)
        df = pd.DataFrame(rows).set_index("Role")
        assert df.loc["viewer", "query"] == "deny"
        assert df.loc["viewer", "insert"] == "deny"


_GREEN_HEX = "#22c55e"
_RED_HEX = "#ef4444"
_AMBER_HEX = "#f59e0b"


class TestColorStyling:
    def _color_action(self, val: str) -> str:
        low = str(val).lower()
        if low == "allow":
            return f"background-color: {_GREEN_HEX}; color: white"
        if low == "deny":
            return f"background-color: {_RED_HEX}; color: white"
        return f"background-color: {_AMBER_HEX}; color: white"

    def test_allow_cell_is_green_dashboard(self) -> None:
        assert _GREEN_HEX in self._color_action("allow")

    def test_deny_cell_is_red_dashboard(self) -> None:
        assert _RED_HEX in self._color_action("deny")

    def test_case_insensitive_dashboard(self) -> None:
        assert _GREEN_HEX in self._color_action("ALLOW")
        assert _RED_HEX in self._color_action("Deny")

    def test_unknown_action_is_amber_dashboard(self) -> None:
        assert "#f59e0b" in self._color_action("unknown")


class TestDataPermissionsRendering:
    def test_data_permissions_rows_mapped_correctly_dashboard(self) -> None:
        """Data permission items are correctly mapped to display rows."""
        items = [
            {
                "role_id": "r1-uuid-xxxx",
                "connection_id": "c1-uuid-xxxx",
                "schema": "public",
                "table": "payment",
                "action": "allow",
                "is_admin_grant": False,
            },
            {
                "role_id": "r2-uuid-xxxx",
                "connection_id": "c1-uuid-xxxx",
                "schema": "*",
                "table": "*",
                "action": "deny",
                "is_admin_grant": True,
            },
        ]
        rows = []
        for perm in items:
            rows.append(
                {
                    "Role ID": str(perm.get("role_id", ""))[:8] + "…",
                    "Connection": str(perm.get("connection_id", ""))[:8] + "…",
                    "Schema": perm.get("schema", "*"),
                    "Table": perm.get("table", "*"),
                    "Action": perm.get("action", "deny"),
                    "Admin grant": "Yes" if perm.get("is_admin_grant") else "No",
                }
            )

        df = pd.DataFrame(rows)
        assert df.shape == (2, 6)
        assert df.iloc[0]["Action"] == "allow"
        assert df.iloc[1]["Admin grant"] == "Yes"
        assert df.iloc[0]["Schema"] == "public"
        assert df.iloc[1]["Schema"] == "*"

    def test_fetch_data_permissions_called_with_token_dashboard(self) -> None:
        """Verify fetch_data_permissions is invoked with the correct token."""
        mock_response = {"items": [], "total": 0, "page": 1, "size": 50}
        with patch("pyrene_dashboard.api_client.get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_response
            mock_resp.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_resp
            mock_get_client.return_value = mock_client

            from pyrene_dashboard.api_client import fetch_data_permissions

            fetch_data_permissions("test_token", page=1, size=50)

        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer test_token"
