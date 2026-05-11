"""Integration tests for `POST /agents/{spec_id}/run`.

Uses Pydantic AI's `TestModel` override to avoid hitting a real model API
in CI (mirrors PLAN-001 agent test pattern). Verifies:

  - admin + analyst can run; viewer is 403.
  - Cross-team spec_id → 404 (enumeration defense).
  - Successful run returns an `AnalystResponse`-shaped payload with
    request_id stamped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic_ai import models as pyd_ai_models
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
        secret="agents-run-test-secret-with-thirty-two-plus-bytes-aaaa",
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
    """Replace the live model with TestModel for the duration of each test.

    Pydantic AI v1.93 + `defer_model_check=True`: at run time the agent
    looks up its model via the global override (`override_allow_model_requests`)
    or its `model` attribute. We use `models.override_allow_model_requests`
    sentinel by force-setting the agent's model on the canonical
    `sql_analyst` instance.
    """
    from pyrene_sql import agent as agent_module

    # `call_tools=[]`: TestModel will go straight to the terminal output_type
    # (AnalystResponse) without invoking any of the @agent.tool callbacks.
    # This keeps the test focused on the spec/run wiring and isolates from
    # the SQL tool path (which would require seeded DVD-Rental rows).
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
    """Seed a user + named role on the default team. Returns (token, user_id, team_id)."""
    async with factory() as s:
        # Idempotent role / team lookups. Using distinct result variables
        # so mypy can narrow each `scalar_one_or_none()` independently.
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
        # No tools — TestModel can satisfy AnalystResponse output_type
        # directly without calling a real DB tool, which keeps the test
        # focused on the spec/run wiring.
        "tools": [],
    }


# -------------------- Permission gating --------------------


async def test_viewer_cannot_run_agent(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
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
        f"/agents/{spec_id}/run",
        headers={"Authorization": f"Bearer {viewer_token}"},
        json={"question": "How many films?"},
    )
    assert response.status_code == 403


# -------------------- Cross-team isolation --------------------


async def test_other_team_spec_returns_404(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin_token, _, _ = await _seed_user_with_role(
        session_factory, jwt_settings, "admin"
    )
    created = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=_spec_body(),
    )
    spec_id = created.json()["id"]

    # Build a second team + admin and try to run a spec in the first team.
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
        f"/agents/{spec_id}/run",
        headers={"Authorization": f"Bearer {other_token}"},
        json={"question": "How many films?"},
    )
    assert response.status_code == 404


# -------------------- Successful run --------------------


async def test_signup_create_run_flow(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """End-to-end Day 3 integration: admin creates spec → analyst runs it."""
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
        f"/agents/{spec_id}/run",
        headers={"Authorization": f"Bearer {analyst_token}"},
        json={"question": "How many films?"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # AnalystResponse fields:
    assert "confidence" in body
    assert "request_id" in body
    # request_id is a UUIDv4 — parse to validate.
    UUID(body["request_id"])


async def test_run_missing_spec_returns_404(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    analyst_token, _, _ = await _seed_user_with_role(
        session_factory, jwt_settings, "analyst"
    )
    response = await client.post(
        f"/agents/{uuid4()}/run",
        headers={"Authorization": f"Bearer {analyst_token}"},
        json={"question": "anything"},
    )
    assert response.status_code == 404


# Silence unused-import warnings for diagnostics-only imports.
_ = pyd_ai_models
