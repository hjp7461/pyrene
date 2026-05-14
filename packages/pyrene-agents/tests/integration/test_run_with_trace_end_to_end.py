"""PRD-046 §7.3 — end-to-end integration for `POST /agents/{spec_id}/run-with-trace`.

Mirrors `test_run_api.py` patterns (TestModel override + `_seed_user_with_role`)
since the wider hook chain (BUDGET → RBAC → AUDIT → COST) only fires when an
actual tool call is dispatched via Gateway. The 3 scenarios here verify the
*sibling-endpoint contract* — endpoint-level role gating, cross-team
isolation, and the new observability response shape — not the audit-chain
correlation (covered by Phase 3.2 follow-up; see `cost_usd`/`audit_id` will be
`None` until a real LLM + tool path is wired).

Scenarios:
  1. viewer JWT → 403 (require_any_role("admin", "analyst")).
  2. cross-team analyst JWT → 404 (enumeration defense).
  3. analyst JWT → 200 with the 3 additive fields present
     (audit_id / cost_usd / logfire_trace_url; None expected under TestModel).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic_ai.models.test import TestModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from pyrene_agents.app import make_app
from pyrene_auth.hashing import hash_password
from pyrene_auth.jwt import JwtSettings, make_access_token
from pyrene_auth.models import Role, Team, User, UserTeamRole
from pyrene_auth.settings import AuthSettings

pytestmark = pytest.mark.integration


# -------------------- fixtures (mirror test_run_api.py) --------------------


@pytest_asyncio.fixture
async def app_engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(migrated_db, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def cleanup_db(app_engine: AsyncEngine) -> AsyncIterator[None]:
    async with app_engine.begin() as conn:
        await conn.execute(text("SET LOCAL audit.bypass = 'on'"))
        await conn.execute(
            text(
                "TRUNCATE TABLE agent_versions, agent_specs, "
                "user_team_roles, users, teams, roles "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def jwt_settings() -> JwtSettings:
    return JwtSettings(
        secret="run-with-trace-test-secret-with-thirty-two-plus-bytes-aa",
        access_ttl_seconds=900,
        refresh_ttl_seconds=604800,
    )


@pytest_asyncio.fixture
async def session_factory(
    app_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
def patched_model(monkeypatch: pytest.MonkeyPatch) -> TestModel:
    """Replace the live model with TestModel (call_tools=[] keeps focus on wiring)."""
    from pyrene_sql import agent as agent_module

    fake = TestModel(call_tools=[])
    monkeypatch.setattr(agent_module.sql_analyst, "model", fake)
    return fake


@pytest_asyncio.fixture
async def app(
    app_engine: AsyncEngine,
    jwt_settings: JwtSettings,
    cleanup_db: None,
    session_factory: async_sessionmaker[AsyncSession],
    patched_model: TestModel,
) -> FastAPI:
    async def session_dep() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    return make_app(
        auth_settings=AuthSettings(pg_dsn="x", enumeration_defense_ms=10),
        jwt_settings=jwt_settings,
        session_dep=session_dep,
    )


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_user_with_role(
    factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
    role_name: str,
) -> tuple[str, UUID, UUID]:
    async with factory() as s:
        team_result = await s.execute(select(Team).where(Team.name == "default"))
        team = team_result.scalar_one_or_none()
        if team is None:
            team = Team(name="default")
            s.add(team)
            await s.flush()
        role_result = await s.execute(select(Role).where(Role.name == role_name))
        role = role_result.scalar_one_or_none()
        if role is None:
            role = Role(name=role_name, description="")
            s.add(role)
            await s.flush()
        user = User(
            email=f"{role_name}@example.com",
            password_hash=hash_password("pw1234567"),
        )
        s.add(user)
        await s.flush()
        s.add(UserTeamRole(user_id=user.id, team_id=team.id, role_id=role.id))
        await s.commit()
        token = make_access_token(user.id, team.id, (role_name,), jwt_settings)
        return token, user.id, team.id


def _spec_body() -> dict[str, object]:
    return {
        "name": "sql-analyst",
        "description": "phase 1",
        "system_prompt": "You are a SQL analyst.",
        "output_schema_key": "AnalystResponse",
        "tools": [],
    }


# -------------------- 1. Role gating --------------------


async def test_viewer_cannot_run_with_trace(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """Viewer is excluded by `require_any_role('admin', 'analyst')` (PRD-046 §4.1)."""
    admin_token, _, _ = await _seed_user_with_role(
        session_factory, jwt_settings, "admin"
    )
    viewer_token, _, _ = await _seed_user_with_role(
        session_factory, jwt_settings, "viewer"
    )
    created = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=_spec_body(),
    )
    spec_id = created.json()["id"]

    response = await client.post(
        f"/agents/{spec_id}/run-with-trace",
        headers={"Authorization": f"Bearer {viewer_token}"},
        json={"question": "How many films?"},
    )
    assert response.status_code == 403


# -------------------- 2. Cross-team isolation --------------------


async def test_other_team_spec_returns_404_with_trace(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """Spec in another team → 404 (enumeration defense — same path as /run)."""
    admin_token, _, _ = await _seed_user_with_role(
        session_factory, jwt_settings, "admin"
    )
    created = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=_spec_body(),
    )
    spec_id = created.json()["id"]

    async with session_factory() as s:
        other_team = Team(name="other-team")
        other_user = User(
            email="other@example.com",
            password_hash=hash_password("pw1234567"),
        )
        s.add_all([other_team, other_user])
        await s.flush()
        result = await s.execute(select(Role).where(Role.name == "admin"))
        admin_role = result.scalar_one()
        s.add(
            UserTeamRole(
                user_id=other_user.id,
                team_id=other_team.id,
                role_id=admin_role.id,
            )
        )
        await s.commit()
        other_token = make_access_token(
            other_user.id, other_team.id, ("admin",), jwt_settings
        )

    response = await client.post(
        f"/agents/{spec_id}/run-with-trace",
        headers={"Authorization": f"Bearer {other_token}"},
        json={"question": "How many films?"},
    )
    assert response.status_code == 404


# -------------------- 3. Observability fields shape --------------------


async def test_analyst_run_with_trace_returns_observability_fields(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """Analyst → 200 with audit_id / cost_usd / logfire_trace_url fields present.

    Under TestModel(call_tools=[]) the hook chain never fires (no tool dispatch
    via Gateway), so the 3 additive fields are expected to be None. The
    contract this test enforces is *additive schema compatibility*: every
    response carries the 3 fields, callers can rely on the keys existing.
    """
    admin_token, _, _ = await _seed_user_with_role(
        session_factory, jwt_settings, "admin"
    )
    analyst_token, _, _ = await _seed_user_with_role(
        session_factory, jwt_settings, "analyst"
    )

    created = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=_spec_body(),
    )
    assert created.status_code == 201
    spec_id = created.json()["id"]

    response = await client.post(
        f"/agents/{spec_id}/run-with-trace",
        headers={"Authorization": f"Bearer {analyst_token}"},
        json={"question": "How many films?"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # AnalystResponse-inherited fields
    assert "confidence" in body
    assert "attempts" in body

    # AnalystResponseWithObservability additive fields — keys MUST be present,
    # values may be None when hook chain has not fired (TestModel scope).
    assert "audit_id" in body
    assert "cost_usd" in body
    assert "logfire_trace_url" in body


async def test_run_with_trace_missing_spec_returns_404(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    analyst_token, _, _ = await _seed_user_with_role(
        session_factory, jwt_settings, "analyst"
    )
    response = await client.post(
        f"/agents/{uuid4()}/run-with-trace",
        headers={"Authorization": f"Bearer {analyst_token}"},
        json={"question": "anything"},
    )
    assert response.status_code == 404
