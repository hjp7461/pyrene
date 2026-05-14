"""Authentication gate for the Pyrene MCP frontend.

Mirrors `pyrene-dashboard.auth` but admits BOTH `admin` and `analyst`
roles (matches the gateway's `_require_reader = require_any_role(
"admin", "analyst")` for `/gateway/servers/*` endpoints — F-15 / ADR-019
keeps the frontend role check coarse and lets the gateway hook chain
do the fine-grained per-tool RBAC).
"""

from __future__ import annotations

import time

import streamlit as st

from pyrene_mcp_frontend.api_client import fetch_me, friendly_error

_SESSION_TTL_SECONDS = 3600
_ALLOWED_ROLES: frozenset[str] = frozenset({"admin", "analyst"})


def _clear_session() -> None:
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


def is_authenticated() -> bool:
    if "auth_token" not in st.session_state:
        return False
    if _is_session_expired():
        _clear_session()
        return False
    return True


def show_login() -> None:
    """Render the token-paste login form in the sidebar."""
    st.title("Pyrene MCP Frontend")
    st.info("MCP 도구를 사용할 admin 또는 analyst JWT access token을 붙여 넣으세요.")

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
            if not _ALLOWED_ROLES.intersection(roles):
                st.error(
                    "MCP 도구 사용 권한이 없는 토큰입니다 — admin 또는 analyst 역할이 필요합니다"
                )
                return

            _store_session(raw, roles)
            st.rerun()


def show_logout_button() -> None:
    with st.sidebar:
        st.divider()
        if st.button("Logout", key="_logout_btn", type="secondary"):
            _clear_session()
            st.rerun()


def require_mcp_user() -> str:
    """Gate: ensure current user has admin or analyst role.

    Returns the JWT token string if all checks pass.
    Calls `st.stop()` otherwise (rendering halted for this run).
    """
    if _is_session_expired():
        _clear_session()
        st.warning("세션이 만료되었습니다. 다시 로그인하세요.")
        show_login()
        st.stop()

    if not is_authenticated():
        show_login()
        st.stop()

    token: str = st.session_state["auth_token"]
    return token


__all__ = [
    "is_authenticated",
    "require_mcp_user",
    "show_login",
    "show_logout_button",
]
