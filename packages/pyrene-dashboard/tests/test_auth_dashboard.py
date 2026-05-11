"""Unit tests for pyrene_dashboard.auth module.

Uses ``unittest.mock.patch`` to avoid real HTTP calls.
Test naming uses ``_dashboard`` suffix per PLAN-016 convention.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_session_state(**kwargs: object) -> dict[str, object]:
    """Return a fresh dict that mimics st.session_state for patching."""
    base: dict[str, object] = {}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# is_authenticated
# ---------------------------------------------------------------------------


class TestIsAuthenticated:
    def test_returns_false_when_no_token_dashboard(self) -> None:
        """No token in session → is_authenticated() is False."""
        with patch("streamlit.session_state", {}):
            from pyrene_dashboard.auth import is_authenticated

            assert is_authenticated() is False

    def test_returns_true_when_token_present_not_expired_dashboard(self) -> None:
        """Valid token + future expires_at → is_authenticated() is True."""
        state = {
            "auth_token": "tok123",
            "auth_role": ["admin"],
            "auth_expires_at": time.time() + 3600,
        }
        with patch("streamlit.session_state", state):
            from pyrene_dashboard.auth import is_authenticated

            assert is_authenticated() is True

    def test_returns_false_when_expired_and_clears_session_dashboard(self) -> None:
        """Expired session → is_authenticated() returns False + clears state."""
        state: dict[str, object] = {
            "auth_token": "old_tok",
            "auth_role": ["admin"],
            "auth_expires_at": time.time() - 1,  # already expired
        }
        with patch("streamlit.session_state", state):
            from pyrene_dashboard.auth import is_authenticated

            result = is_authenticated()
        assert result is False
        assert "auth_token" not in state


# ---------------------------------------------------------------------------
# require_admin — needs st.stop() patched so it raises instead of blocking
# ---------------------------------------------------------------------------


class TestRequireAdmin:
    def test_returns_token_for_authenticated_admin_dashboard(self) -> None:
        """Authenticated admin session → require_admin() returns token."""
        state: dict[str, object] = {
            "auth_token": "admin_jwt",
            "auth_role": ["admin"],
            "auth_expires_at": time.time() + 3600,
        }
        with (
            patch("streamlit.session_state", state),
            patch("streamlit.stop", side_effect=SystemExit("stop")),
            patch("streamlit.warning"),
            patch("streamlit.error"),
        ):
            from pyrene_dashboard.auth import require_admin

            token = require_admin()
        assert token == "admin_jwt"

    def test_calls_stop_when_unauthenticated_dashboard(self) -> None:
        """No token → show_login() called + st.stop() invoked."""
        with (
            patch("streamlit.session_state", {}),
            patch("streamlit.stop", side_effect=SystemExit("stop")),
            patch("streamlit.title"),
            patch("streamlit.info"),
            patch("streamlit.sidebar"),
            patch("streamlit.text_input", return_value=""),
            patch("streamlit.button", return_value=False),
        ):
            from pyrene_dashboard.auth import require_admin

            with pytest.raises(SystemExit, match="stop"):
                require_admin()

    def test_auto_logout_on_expired_session_dashboard(self) -> None:
        """Expired session → auto-logout warning + st.stop()."""
        state: dict[str, object] = {
            "auth_token": "expired",
            "auth_role": ["admin"],
            "auth_expires_at": time.time() - 10,
        }
        with (
            patch("streamlit.session_state", state),
            patch("streamlit.stop", side_effect=SystemExit("stop")),
            patch("streamlit.warning"),
            patch("streamlit.title"),
            patch("streamlit.info"),
            patch("streamlit.sidebar"),
            patch("streamlit.text_input", return_value=""),
            patch("streamlit.button", return_value=False),
        ):
            from pyrene_dashboard.auth import require_admin

            with pytest.raises(SystemExit, match="stop"):
                require_admin()


# ---------------------------------------------------------------------------
# Login form — non-admin token
# ---------------------------------------------------------------------------


class TestShowLogin:
    def test_non_admin_token_shows_error_dashboard(self) -> None:
        """Non-admin roles on /auth/me → error shown, _store_session NOT called.

        Patches ``pyrene_dashboard.auth.fetch_me`` (the name as imported into
        auth.py) so the @st.cache_data wrapper is bypassed entirely.
        """
        mock_me = {"id": "u1", "email": "user@example.com", "team_id": "t1", "roles": ["viewer"]}

        with (
            # Patch the name as used inside auth.py (imported reference)
            patch("pyrene_dashboard.auth.fetch_me", return_value=mock_me),
            patch("streamlit.error") as mock_error,
            patch("streamlit.button", return_value=True),  # simulate click
            patch("streamlit.text_input", return_value="viewer_token"),
            patch("streamlit.header"),
            patch("streamlit.rerun") as mock_rerun,
            patch("streamlit.title"),
            patch("streamlit.info"),
            patch("pyrene_dashboard.auth._store_session") as mock_store,
            patch(
                "streamlit.sidebar",
                MagicMock(
                    __enter__=MagicMock(return_value=None),
                    __exit__=MagicMock(return_value=False),
                ),
            ),
        ):
            from pyrene_dashboard.auth import show_login

            show_login()

        # st.error must have been called with admin-access message
        assert mock_error.called
        error_msg: str = str(mock_error.call_args[0][0])
        assert "Admin" in error_msg or "admin" in error_msg
        # Session store must NOT have been called
        assert not mock_store.called
        # rerun must NOT have been called
        assert not mock_rerun.called

    def test_admin_token_stores_session_and_reruns_dashboard(self) -> None:
        """Admin token → _store_session called, st.rerun() called.

        Patches ``pyrene_dashboard.auth.fetch_me`` (the name as imported into
        auth.py) so the @st.cache_data wrapper is bypassed entirely.
        """
        mock_me = {"id": "u1", "email": "admin@example.com", "team_id": "t1", "roles": ["admin"]}

        with (
            # Patch the name as used inside auth.py (imported reference)
            patch("pyrene_dashboard.auth.fetch_me", return_value=mock_me),
            patch("streamlit.error"),
            patch("streamlit.button", return_value=True),
            patch("streamlit.text_input", return_value="admin_jwt_token"),
            patch("streamlit.header"),
            patch("streamlit.rerun") as mock_rerun,
            patch("streamlit.title"),
            patch("streamlit.info"),
            patch("pyrene_dashboard.auth._store_session") as mock_store,
            patch(
                "streamlit.sidebar",
                MagicMock(
                    __enter__=MagicMock(return_value=None),
                    __exit__=MagicMock(return_value=False),
                ),
            ),
        ):
            from pyrene_dashboard.auth import show_login

            show_login()

        # _store_session must have been called with the token and admin role
        assert mock_store.called
        call_args = mock_store.call_args
        assert call_args[0][0] == "admin_jwt_token"
        assert "admin" in call_args[0][1]
        assert mock_rerun.called
