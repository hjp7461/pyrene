"""PRD-046 §4.1 — /run-with-trace sibling handler 단위 테스트.

기존 /run 핸들러의 invariant 보존 + observability augmentation 검증.
TestClient + dependency_overrides (no real LLM / DB / Anthropic).

Mock strategy (2-layer):
  1. FastAPI `dependency_overrides` — overrides `_require_runner` (skips JWT +
     DB auth) and `_session_proxy` (injects a mock AsyncSession).
  2. `unittest.mock.patch` — overrides route-level callables:
       - `get_spec_for_team`, `get_latest_version`, `build_agent` (repo layer)
       - `run_with_retry`, `lookup_audit_event_id`, `lookup_cost_usd`,
         `build_logfire_trace_url` (agent + observability layer)
     Patching at the `pyrene_agents.routes.run` module namespace ensures the
     handler picks up the mock regardless of import alias.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_agents.app import make_app
from pyrene_agents.routes import run as _run_module
from pyrene_auth.dependencies import _session_proxy as _real_session_proxy
from pyrene_auth.jwt import JwtSettings
from pyrene_core import Confidence, UserContext
from pyrene_sql.agent import AnalystResponse
from pyrene_sql.retry import AttemptTrace

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_ROUTES_MODULE = "pyrene_agents.routes.run"

_TEAM_ID = uuid4()
_USER_ID = uuid4()

_USER_CTX = UserContext(user_id=_USER_ID, team_id=_TEAM_ID, roles=("admin",))


# ──────────────────────────────────────────────────────────────────────────────
# Minimal fakes for spec / version
# ──────────────────────────────────────────────────────────────────────────────


class _FakeSpec:
    id = uuid4()
    team_id = _TEAM_ID
    name = "test-spec"


class _FakeVersion:
    id = uuid4()
    version = 1
    output_schema_key = "AnalystResponse"
    system_prompt = "You are a SQL analyst."
    tools: tuple[str, ...] = ()


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


@pytest.fixture()
def app(mock_session: AsyncMock) -> FastAPI:
    """Build the FastAPI app with auth dependencies overridden to avoid DB."""

    # Provide a session_dep so make_app skips real engine construction.
    async def _fake_session_dep() -> AsyncIterator[AsyncSession]:
        yield mock_session

    built = make_app(
        jwt_settings=JwtSettings(
            secret="unit-test-secret-for-pyrene-with-thirty-two-plus-bytes",
        ),
        session_dep=_fake_session_dep,
    )

    # Override _require_runner to skip JWT decode + DB role re-read.
    def _fake_auth() -> UserContext:
        return _USER_CTX

    built.dependency_overrides[_run_module._require_runner] = _fake_auth
    built.dependency_overrides[_real_session_proxy] = _fake_session_dep
    return built


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_analyst_response(
    *,
    sql: str | None = "SELECT 1",
    attempts: tuple[AttemptTrace, ...] = (),
) -> AnalystResponse:
    return AnalystResponse(
        sql=sql,
        rows=[{"count": 42}],
        row_count=1,
        analysis="ok",
        confidence=Confidence.high,
        attempts=attempts,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Tests — /run-with-trace
# ──────────────────────────────────────────────────────────────────────────────


def test_run_with_trace_returns_observability_fields(app: FastAPI) -> None:
    """정상 경로: 3 observability 필드가 모두 populate 된다."""
    audit_uuid = uuid4()
    spec_uuid = uuid4()

    with (
        patch(f"{_ROUTES_MODULE}.get_spec_for_team", new_callable=AsyncMock) as m_spec,
        patch(f"{_ROUTES_MODULE}.get_latest_version", new_callable=AsyncMock) as m_ver,
        patch(f"{_ROUTES_MODULE}.build_agent") as m_build,
        patch(f"{_ROUTES_MODULE}.run_with_retry", new_callable=AsyncMock) as m_retry,
        patch(f"{_ROUTES_MODULE}.lookup_audit_event_id", new_callable=AsyncMock) as m_audit,
        patch(f"{_ROUTES_MODULE}.lookup_cost_usd", new_callable=AsyncMock) as m_cost,
        patch(f"{_ROUTES_MODULE}.build_logfire_trace_url") as m_url,
    ):
        m_spec.return_value = _FakeSpec()
        m_ver.return_value = _FakeVersion()
        m_build.return_value = object()
        m_retry.return_value = _make_analyst_response()
        m_audit.return_value = audit_uuid
        m_cost.return_value = Decimal("0.00123")
        m_url.return_value = "https://logfire.example/traces/abc"

        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                f"/agents/{spec_uuid}/run-with-trace",
                json={"question": "How many films?"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # base AnalystResponse fields preserved
    assert body["confidence"] == "high"
    assert body["sql"] == "SELECT 1"

    # observability augmentation
    assert body["audit_id"] == str(audit_uuid)
    assert body["cost_usd"] == "0.00123"
    assert body["logfire_trace_url"] == "https://logfire.example/traces/abc"


def test_run_with_trace_audit_miss_graceful(app: FastAPI) -> None:
    """audit lookup None → audit_id 필드 null, cost + url 는 정상."""
    spec_uuid = uuid4()

    with (
        patch(f"{_ROUTES_MODULE}.get_spec_for_team", new_callable=AsyncMock) as m_spec,
        patch(f"{_ROUTES_MODULE}.get_latest_version", new_callable=AsyncMock) as m_ver,
        patch(f"{_ROUTES_MODULE}.build_agent") as m_build,
        patch(f"{_ROUTES_MODULE}.run_with_retry", new_callable=AsyncMock) as m_retry,
        patch(f"{_ROUTES_MODULE}.lookup_audit_event_id", new_callable=AsyncMock) as m_audit,
        patch(f"{_ROUTES_MODULE}.lookup_cost_usd", new_callable=AsyncMock) as m_cost,
        patch(f"{_ROUTES_MODULE}.build_logfire_trace_url") as m_url,
    ):
        m_spec.return_value = _FakeSpec()
        m_ver.return_value = _FakeVersion()
        m_build.return_value = object()
        m_retry.return_value = _make_analyst_response()
        m_audit.return_value = None  # <── miss
        m_cost.return_value = Decimal("0.00050")
        m_url.return_value = "https://logfire.example/traces/def"

        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                f"/agents/{spec_uuid}/run-with-trace",
                json={"question": "How many actors?"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["audit_id"] is None
    assert body["cost_usd"] == "0.00050"
    assert body["logfire_trace_url"] == "https://logfire.example/traces/def"


def test_run_with_trace_logfire_unset_graceful(app: FastAPI) -> None:
    """LOGFIRE_URL 미설정 → logfire_trace_url None, 나머지 정상."""
    spec_uuid = uuid4()
    audit_uuid = uuid4()

    with (
        patch(f"{_ROUTES_MODULE}.get_spec_for_team", new_callable=AsyncMock) as m_spec,
        patch(f"{_ROUTES_MODULE}.get_latest_version", new_callable=AsyncMock) as m_ver,
        patch(f"{_ROUTES_MODULE}.build_agent") as m_build,
        patch(f"{_ROUTES_MODULE}.run_with_retry", new_callable=AsyncMock) as m_retry,
        patch(f"{_ROUTES_MODULE}.lookup_audit_event_id", new_callable=AsyncMock) as m_audit,
        patch(f"{_ROUTES_MODULE}.lookup_cost_usd", new_callable=AsyncMock) as m_cost,
        patch(f"{_ROUTES_MODULE}.build_logfire_trace_url") as m_url,
    ):
        m_spec.return_value = _FakeSpec()
        m_ver.return_value = _FakeVersion()
        m_build.return_value = object()
        m_retry.return_value = _make_analyst_response()
        m_audit.return_value = audit_uuid
        m_cost.return_value = Decimal("0.00070")
        m_url.return_value = None  # <── env unset

        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                f"/agents/{spec_uuid}/run-with-trace",
                json={"question": "How many customers?"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["audit_id"] == str(audit_uuid)
    assert body["cost_usd"] == "0.00070"
    assert body["logfire_trace_url"] is None


def test_run_with_trace_preserves_attempts(app: FastAPI) -> None:
    """run_with_retry 가 반환한 attempts 가 response 에 그대로 전달된다."""
    spec_uuid = uuid4()

    attempt = AttemptTrace(sql="SELECT bad", error="syntax error", duration_ms=10)
    analyst_resp = _make_analyst_response(attempts=(attempt,))

    with (
        patch(f"{_ROUTES_MODULE}.get_spec_for_team", new_callable=AsyncMock) as m_spec,
        patch(f"{_ROUTES_MODULE}.get_latest_version", new_callable=AsyncMock) as m_ver,
        patch(f"{_ROUTES_MODULE}.build_agent") as m_build,
        patch(f"{_ROUTES_MODULE}.run_with_retry", new_callable=AsyncMock) as m_retry,
        patch(f"{_ROUTES_MODULE}.lookup_audit_event_id", new_callable=AsyncMock) as m_audit,
        patch(f"{_ROUTES_MODULE}.lookup_cost_usd", new_callable=AsyncMock) as m_cost,
        patch(f"{_ROUTES_MODULE}.build_logfire_trace_url") as m_url,
    ):
        m_spec.return_value = _FakeSpec()
        m_ver.return_value = _FakeVersion()
        m_build.return_value = object()
        m_retry.return_value = analyst_resp
        m_audit.return_value = None
        m_cost.return_value = None
        m_url.return_value = None

        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                f"/agents/{spec_uuid}/run-with-trace",
                json={"question": "retry test"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    attempts = body["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["sql"] == "SELECT bad"
    assert attempts[0]["error"] == "syntax error"
    assert attempts[0]["duration_ms"] == 10


def test_run_endpoint_unchanged_no_observability_fields(app: FastAPI) -> None:
    """기존 /run endpoint — audit_id / cost_usd / logfire_trace_url 필드 없음.

    additive-only contract 검증: /run 응답 payload 에 새 필드가 없어야 한다.
    """
    spec_uuid = uuid4()
    analyst_resp = _make_analyst_response()

    class _FakeRunResult:
        output = analyst_resp

    with (
        patch(f"{_ROUTES_MODULE}.get_spec_for_team", new_callable=AsyncMock) as m_spec,
        patch(f"{_ROUTES_MODULE}.get_latest_version", new_callable=AsyncMock) as m_ver,
        patch(f"{_ROUTES_MODULE}.build_agent") as m_build,
        patch(f"{_ROUTES_MODULE}.sql_analyst") as m_sql_analyst,
    ):
        m_spec.return_value = _FakeSpec()
        m_ver.return_value = _FakeVersion()
        m_build.return_value = object()
        m_sql_analyst.run = AsyncMock(return_value=_FakeRunResult())

        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                f"/agents/{spec_uuid}/run",
                json={"question": "additive-only check"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Observability fields must be ABSENT from the plain /run response.
    assert "audit_id" not in body
    assert "cost_usd" not in body
    assert "logfire_trace_url" not in body

    # But request_id should still be present (stamped by existing handler).
    assert "request_id" in body
    UUID(body["request_id"])  # validates it's a proper UUID


__all__: list[str] = []
