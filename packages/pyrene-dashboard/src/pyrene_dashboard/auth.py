"""Authentication and session management for the Pyrene admin dashboard.

Design (PM Wave 0' amend):
- Token is pasted in the sidebar as ``st.text_input(type="password")``.
- On paste, ``GET /auth/me`` is called to validate + extract roles.
- Admin gate: only users whose ``roles`` list contains ``"admin"`` pass.
  Non-admin → 한국어 오류 메시지 ("관리자(admin) 권한이 없는 토큰입니다 …") +
  ``return``. 메시지 매핑은 PRD-020 / ``api_client.friendly_error`` 참조.
- Session TTL: ``st.session_state["auth_expires_at"]`` is set to
  ``now + 3600 s`` (1 h) on successful login. Every call to
  ``ensure_authenticated()`` checks expiry and auto-logs out if expired.
- Logout: ``del st.session_state["auth_token"]`` + ``st.rerun()``.

Usage
-----
    from pyrene_dashboard import auth

    # At the top of every page:
    token = auth.require_admin()
    # ``token`` is the raw JWT — pass it to api_client functions.
"""

from __future__ import annotations

import time

import streamlit as st

from pyrene_dashboard.api_client import fetch_me, friendly_error

# Number of seconds a pasted token session is trusted locally.
# The real expiry is encoded in the JWT; this is a UX safety net.
_SESSION_TTL_SECONDS = 3600


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clear_session() -> None:
    """Remove all auth-related keys from ``st.session_state``."""
    for key in ("auth_token", "auth_role", "auth_expires_at"):
        st.session_state.pop(key, None)


def _is_session_expired() -> bool:
    expires_at = st.session_state.get("auth_expires_at")
    if expires_at is None:
        return False
    return float(expires_at) < time.time()


def _store_session(token: str, roles: list[str]) -> None:
    st.session_state["auth_token"] = token
    st.session_state["auth_role"] = roles
    st.session_state["auth_expires_at"] = time.time() + _SESSION_TTL_SECONDS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_authenticated() -> bool:
    """Return True iff a valid, non-expired token is stored in session_state."""
    if "auth_token" not in st.session_state:
        return False
    if _is_session_expired():
        _clear_session()
        return False
    return True


def show_login() -> None:
    """Render the token-paste login form in the sidebar.

    On successful login + admin validation, stores the session and
    triggers ``st.rerun()`` so the caller's ``st.stop()`` is bypassed
    on the next run.
    """
    st.title("Pyrene Admin Dashboard")
    st.info("Paste your admin JWT access token to continue.")

    with st.sidebar:
        st.header("Login")
        token_input = st.text_input(
            "Access token",
            type="password",
            key="_login_token_input",
            placeholder="Bearer eyJ…",
        )

        if st.button("Sign in", key="_login_btn"):
            raw = token_input.strip()
            if raw.startswith("Bearer "):
                raw = raw[len("Bearer ") :]
            if not raw:
                st.error(
                    "토큰을 입력하세요 — 관리자에게서 발급받은 JWT access token이 필요합니다"
                )
                return

            try:
                me = fetch_me(raw)
            except Exception as exc:
                st.error(friendly_error(exc, context="인증"))
                return

            roles: list[str] = me.get("roles", [])
            if "admin" not in roles:
                st.error(
                    "관리자(admin) 권한이 없는 토큰입니다 — 관리자에게 권한 부여를 요청하세요"
                )
                return

            _store_session(raw, roles)
            st.rerun()


def show_logout_button() -> None:
    """Render the logout button in the sidebar.

    Clears session state and reruns, returning the user to the login form.
    Must be called from within a ``with st.sidebar`` block or equivalent.
    """
    with st.sidebar:
        st.divider()
        if st.button("Logout", key="_logout_btn", type="secondary"):
            _clear_session()
            st.rerun()


def require_admin() -> str:
    """Gate: ensure current user is authenticated admin.

    Returns the JWT token string if all checks pass.
    Calls ``st.stop()`` otherwise (rendering is halted for this run).

    This function must be called at the top of every page module so that
    partial rendering never occurs for unauthenticated/non-admin visitors.
    """
    # Auto-logout if TTL expired
    if _is_session_expired():
        _clear_session()
        st.warning("Your session has expired. Please log in again.")
        show_login()
        st.stop()

    if not is_authenticated():
        show_login()
        st.stop()

    token: str = st.session_state["auth_token"]
    return token


__all__ = [
    "is_authenticated",
    "require_admin",
    "show_login",
    "show_logout_button",
]
