"""PRD-046 §4.2 — agent_client.run_agent_with_trace unit tests.

Mirrors `test_api_client_helpers.py` MockTransport pattern. The frontend
package is HTTP-only (ADR-019 / F-15) — response is parsed into a local
`AnalystRunResult` dataclass instead of importing the backend schema.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from pyrene_mcp_frontend.agent_client import (
    AgentRunError,
    AnalystRunResult,
    run_agent_with_trace,
)


def test_run_agent_returns_parsed_response() -> None:
    """200 → AnalystRunResult parsed with all observability fields populated."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-jwt"
        assert request.url.path == "/agents/sql_analyst/run-with-trace"
        return httpx.Response(
            200,
            json={
                "confidence": "high",
                "sql": "SELECT 1",
                "rows": [{"x": 1}],
                "row_count": 1,
                "attempts": [],
                "audit_id": "550e8400-e29b-41d4-a716-446655440000",
                "cost_usd": "0.00123",
                "logfire_trace_url": "https://logfire.example/traces/abc",
            },
        )

    transport = httpx.MockTransport(handler)
    with patch(
        "pyrene_mcp_frontend.agent_client._make_client",
        return_value=httpx.Client(transport=transport),
    ):
        resp = run_agent_with_trace(
            question="test",
            jwt="test-jwt",
            api_base="https://api.example",
        )

    assert isinstance(resp, AnalystRunResult)
    assert resp.confidence == "high"
    assert resp.sql == "SELECT 1"
    assert resp.audit_id == "550e8400-e29b-41d4-a716-446655440000"
    assert resp.cost_usd == "0.00123"
    assert resp.logfire_trace_url == "https://logfire.example/traces/abc"


def test_run_agent_4xx_raises_with_status() -> None:
    """4xx → AgentRunError(status_code=403)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "permission denied"})

    transport = httpx.MockTransport(handler)
    with (
        patch(
            "pyrene_mcp_frontend.agent_client._make_client",
            return_value=httpx.Client(transport=transport),
        ),
        pytest.raises(AgentRunError) as exc_info,
    ):
        run_agent_with_trace(
            question="test", jwt="bad", api_base="https://api.example"
        )

    assert exc_info.value.status_code == 403


def test_run_agent_timeout_raises_agentrunerror() -> None:
    """httpx.TimeoutException → AgentRunError (no status_code)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    transport = httpx.MockTransport(handler)
    with (
        patch(
            "pyrene_mcp_frontend.agent_client._make_client",
            return_value=httpx.Client(transport=transport),
        ),
        pytest.raises(AgentRunError) as exc_info,
    ):
        run_agent_with_trace(
            question="test", jwt="x", api_base="https://api.example"
        )

    assert exc_info.value.status_code is None


def test_run_agent_jwt_header_injection() -> None:
    """JWT injected as `Authorization: Bearer <jwt>`."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={
                "confidence": "high",
                "attempts": [],
            },
        )

    transport = httpx.MockTransport(handler)
    with patch(
        "pyrene_mcp_frontend.agent_client._make_client",
        return_value=httpx.Client(transport=transport),
    ):
        run_agent_with_trace(
            question="x", jwt="my-jwt", api_base="https://api.example"
        )

    assert captured["auth"] == "Bearer my-jwt"
