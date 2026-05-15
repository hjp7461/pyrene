"""PRD-046 §4.2 + PRD-055 — agent_client.run_agent_with_trace unit tests.

Mirrors `test_api_client_helpers.py` MockTransport pattern. The frontend
package is HTTP-only (ADR-019 / F-15) — response is parsed into a local
`AnalystRunResult` dataclass instead of importing the backend schema.

PRD-055 / ADR-026: a spec *name* is resolved to its UUID via
`GET /agents/specs` before the run POST. Handlers are path-aware.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from unittest.mock import patch

import httpx
import pytest
from pyrene_mcp_frontend.agent_client import (
    AgentRunError,
    AnalystRunResult,
    run_agent_with_trace,
)

_SPEC_UUID = "11111111-1111-1111-1111-111111111111"
# Canonical phase1 spec name = "sql-analyst" (hyphen) — exporter.PHASE1_SPEC_NAME.
_SPECS_LIST = [
    {"id": _SPEC_UUID, "name": "sql-analyst", "team_id": "t", "latest_version": 1},
    {"id": "22222222-2222-2222-2222-222222222222", "name": "other"},
]

_RunHandler = Callable[[httpx.Request], httpx.Response]


def _patched(
    *,
    run: _RunHandler,
    specs: list[dict[str, object]] | None = None,
    specs_status: int = 200,
) -> AbstractContextManager[object]:
    """Patch `_make_client` with a path-aware MockTransport.

    GET /agents/specs → `specs` list (default `_SPECS_LIST`);
    everything else → `run` handler.
    """
    specs_body = _SPECS_LIST if specs is None else specs

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agents/specs":
            assert request.method == "GET"
            return httpx.Response(specs_status, json=specs_body)
        return run(request)

    transport = httpx.MockTransport(handler)
    return patch(
        "pyrene_mcp_frontend.agent_client._make_client",
        return_value=httpx.Client(transport=transport),
    )


def test_run_agent_returns_parsed_response() -> None:
    """200 → AnalystRunResult parsed with all observability fields populated."""

    def run(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-jwt"
        # name resolved → UUID path
        assert request.url.path == f"/agents/{_SPEC_UUID}/run-with-trace"
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

    with _patched(run=run):
        resp = run_agent_with_trace(
            question="test", jwt="test-jwt", api_base="https://api.example"
        )

    assert isinstance(resp, AnalystRunResult)
    assert resp.confidence == "high"
    assert resp.sql == "SELECT 1"
    assert resp.audit_id == "550e8400-e29b-41d4-a716-446655440000"
    assert resp.cost_usd == "0.00123"
    assert resp.logfire_trace_url == "https://logfire.example/traces/abc"


def test_run_agent_resolves_name_to_uuid() -> None:
    """AC-1: name 'sql_analyst' → UUID from /agents/specs, run POST uses UUID."""
    seen: dict[str, str] = {}

    def run(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"confidence": "high", "attempts": []})

    with _patched(run=run):
        run_agent_with_trace(
            question="q", jwt="j", api_base="https://api.example"
        )

    assert seen["path"] == f"/agents/{_SPEC_UUID}/run-with-trace"


def test_run_agent_uuid_passthrough_skips_resolution() -> None:
    """AC-2: spec_id already a UUID → no /agents/specs call, direct POST."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"confidence": "high", "attempts": []})

    transport = httpx.MockTransport(handler)
    with patch(
        "pyrene_mcp_frontend.agent_client._make_client",
        return_value=httpx.Client(transport=transport),
    ):
        run_agent_with_trace(
            question="q",
            jwt="j",
            api_base="https://api.example",
            spec_id=_SPEC_UUID,
        )

    assert calls == [f"/agents/{_SPEC_UUID}/run-with-trace"]
    assert "/agents/specs" not in calls


def test_run_agent_spec_not_found_raises_actionable() -> None:
    """AC-3: empty specs list → AgentRunError (Korean + next action), no run."""
    run_called = False

    def run(request: httpx.Request) -> httpx.Response:
        nonlocal run_called
        run_called = True
        return httpx.Response(200, json={})

    with _patched(run=run, specs=[]), pytest.raises(AgentRunError) as exc:
        run_agent_with_trace(
            question="q", jwt="j", api_base="https://api.example"
        )

    assert run_called is False
    msg = str(exc.value)
    assert "sql-analyst" in msg
    assert "관리자" in msg
    assert exc.value.status_code is None


def test_run_agent_specs_list_error_wrapped() -> None:
    """GET /agents/specs 5xx → AgentRunError(status_code) before run."""
    with (
        _patched(run=lambda r: httpx.Response(200), specs=[], specs_status=503),
        pytest.raises(AgentRunError) as exc,
    ):
        run_agent_with_trace(
            question="q", jwt="j", api_base="https://api.example"
        )
    assert exc.value.status_code == 503


def test_run_agent_4xx_raises_with_status() -> None:
    """4xx on run → AgentRunError(status_code=403) (specs resolves OK)."""

    def run(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "permission denied"})

    with _patched(run=run), pytest.raises(AgentRunError) as exc_info:
        run_agent_with_trace(
            question="test", jwt="bad", api_base="https://api.example"
        )

    assert exc_info.value.status_code == 403


def test_run_agent_timeout_raises_agentrunerror() -> None:
    """httpx.TimeoutException → AgentRunError (no status_code)."""

    def run(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    with _patched(run=run), pytest.raises(AgentRunError) as exc_info:
        run_agent_with_trace(
            question="test", jwt="x", api_base="https://api.example"
        )

    assert exc_info.value.status_code is None


def test_run_agent_jwt_header_injection() -> None:
    """JWT injected as `Authorization: Bearer <jwt>` on the run POST."""
    captured: dict[str, str] = {}

    def run(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"confidence": "high", "attempts": []})

    with _patched(run=run):
        run_agent_with_trace(
            question="x", jwt="my-jwt", api_base="https://api.example"
        )

    assert captured["auth"] == "Bearer my-jwt"
