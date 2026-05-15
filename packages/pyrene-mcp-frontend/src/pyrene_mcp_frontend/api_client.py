"""HTTP API client for the Pyrene MCP frontend.

Generic HTTP + UX helpers are imported from ``pyrene-ui-common`` (ADR-025
leaf-utility exemption — no longer duplicated from ``pyrene-dashboard``). Two
helpers are wrapped locally to preserve this frontend's existing behavior:

- ``get_client()`` → shared client with ``timeout=30.0`` (invoke can be slower
  than dashboard reads; dashboard uses the 10s default).
- ``friendly_error(...)`` → shared mapping plus the MCP-specific ``{422: ...}``
  status message.

``fetch_me`` is the shared helper; it uses the 10s default client. ``/auth/me``
is a sub-second profile GET, so the 10s timeout is functionally equivalent to
the prior 30s (the 30s was incidental client reuse, not an /auth/me need).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx
import streamlit as st

from pyrene_ui_common import (
    _auth_headers,
    fetch_me,
    fetch_or_stale,
    format_age_korean,
    get_base_url,
)
from pyrene_ui_common import friendly_error as _ui_friendly_error
from pyrene_ui_common import get_client as _ui_get_client

# ---------------------------------------------------------------------------
# Configuration (logfire helpers are MCP-frontend-only — not extracted)
# ---------------------------------------------------------------------------

_DEFAULT_LOGFIRE_URL = "https://logfire.pydantic.dev"


def get_logfire_base_url() -> str:
    return os.environ.get("LOGFIRE_URL", _DEFAULT_LOGFIRE_URL)


def get_client() -> httpx.Client:
    """Shared sync httpx.Client with the MCP-frontend 30s timeout.

    Delegates to ``pyrene_ui_common.get_client`` (``@st.cache_resource`` keyed
    on the timeout, so this is the process-singleton 30s client).
    """
    return _ui_get_client(timeout=30.0)


_MCP_EXTRA_STATUS: Mapping[int, str] = {
    422: "입력값이 올바르지 않습니다 — 인자를 확인하세요",
}


def friendly_error(exc: BaseException, context: str = "데이터") -> str:
    """원본 예외를 한국어 + 다음 행동 메시지로 매핑 (MCP 422 매핑 포함)."""
    return _ui_friendly_error(exc, context, extra_status=_MCP_EXTRA_STATUS)


# ---------------------------------------------------------------------------
# /gateway/servers — list
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_servers(token: str) -> list[dict[str, Any]]:
    client = get_client()
    r = client.get("/gateway/servers", headers=_auth_headers(token))
    r.raise_for_status()
    result: list[dict[str, Any]] = r.json()
    return result


# ---------------------------------------------------------------------------
# /gateway/servers/{id}/tools — list discovered tools
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def fetch_tools(token: str, server_id: str) -> list[dict[str, Any]]:
    client = get_client()
    r = client.get(
        f"/gateway/servers/{server_id}/tools",
        headers=_auth_headers(token),
    )
    r.raise_for_status()
    result: list[dict[str, Any]] = r.json()
    return result


# ---------------------------------------------------------------------------
# /gateway/servers/{id}/tools/{name}/invoke — PRD-040 Wave 1
# ---------------------------------------------------------------------------


def invoke_tool(
    token: str,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """POST /gateway/servers/{id}/tools/{name}/invoke.

    NOT cached: invocation has side-effects (audit row, budget charge).
    """
    client = get_client()
    r = client.post(
        f"/gateway/servers/{server_id}/tools/{tool_name}/invoke",
        headers=_auth_headers(token),
        json={"arguments": arguments},
    )
    r.raise_for_status()
    return dict(r.json())


# ---------------------------------------------------------------------------
# Logfire deep link helper (F-12 signal)
# ---------------------------------------------------------------------------


def logfire_trace_url(trace_id: str) -> str | None:
    """Build a Logfire dashboard URL for the given trace id, or None when
    the trace id is empty (no recording context)."""
    if not trace_id:
        return None
    return f"{get_logfire_base_url().rstrip('/')}/traces/{trace_id}"


__all__ = [
    "fetch_me",
    "fetch_or_stale",
    "fetch_servers",
    "fetch_tools",
    "format_age_korean",
    "friendly_error",
    "get_base_url",
    "get_client",
    "get_logfire_base_url",
    "invoke_tool",
    "logfire_trace_url",
]
