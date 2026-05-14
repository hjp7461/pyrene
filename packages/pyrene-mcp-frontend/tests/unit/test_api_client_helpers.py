"""Unit tests for api_client pure helpers (no Streamlit runtime needed).

friendly_error / format_age_korean / logfire_trace_url do not touch
`st.*` and are testable in isolation.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import httpx
import pytest
from pyrene_mcp_frontend.api_client import (
    format_age_korean,
    friendly_error,
    logfire_trace_url,
)

# ---------------------------------------------------------------------------
# friendly_error
# ---------------------------------------------------------------------------


def test_friendly_error_connect_error_korean() -> None:
    msg = friendly_error(
        httpx.ConnectError("connection refused"), context="MCP 서버"
    )
    assert "MCP 서버" in msg
    assert "연결할 수 없습니다" in msg


def test_friendly_error_read_timeout_korean() -> None:
    msg = friendly_error(httpx.ReadTimeout("timeout"), context="도구 호출")
    assert "지연됩니다" in msg


def test_friendly_error_http_403_korean() -> None:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 403
    exc = httpx.HTTPStatusError(
        "forbidden", request=MagicMock(), response=response
    )
    msg = friendly_error(exc, context="도구 호출")
    assert "권한이 없습니다" in msg
    assert "403" in msg


def test_friendly_error_http_5xx_korean() -> None:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 503
    exc = httpx.HTTPStatusError(
        "down", request=MagicMock(), response=response
    )
    msg = friendly_error(exc, context="도구 호출")
    assert "서버 오류" in msg
    assert "503" in msg


def test_friendly_error_unknown_falls_back() -> None:
    msg = friendly_error(ValueError("x"), context="X")
    assert "X" in msg
    assert "ValueError" in msg


# ---------------------------------------------------------------------------
# format_age_korean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta_sec,expected_substring",
    [
        (0, "방금"),
        (5, "초 전"),
        (90, "분 전"),
        (3700, "시간 전"),
    ],
)
def test_format_age_korean(delta_sec: int, expected_substring: str) -> None:
    ts = time.time() - delta_sec
    assert expected_substring in format_age_korean(ts)


def test_format_age_korean_none() -> None:
    assert format_age_korean(None) == "방금"


# ---------------------------------------------------------------------------
# logfire_trace_url
# ---------------------------------------------------------------------------


def test_logfire_trace_url_empty_returns_none() -> None:
    assert logfire_trace_url("") is None


def test_logfire_trace_url_builds_path() -> None:
    url = logfire_trace_url("abc123")
    assert url is not None
    assert url.endswith("/traces/abc123")
