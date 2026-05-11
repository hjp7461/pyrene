"""Integration tests for the spec CRUD API + INSERT-only role.

Covers:
  - signup → admin grant → create_spec → list/get → POST new version → list versions
  - viewer cannot create a spec (403)
  - cross-team spec_id → 404 (enumeration defense)
  - INSERT-only enforcement: directly attempt UPDATE/DELETE on agent_versions
    via a `pyrene_app` role and assert SQLSTATE 42501 (insufficient_privilege).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from pyrene_agents.app import make_app
from pyrene_agents.models import AgentSpec, AgentVersion
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
        secret="agents-test-secret-with-thirty-two-plus-bytes-aaaaaa",
        access_ttl_seconds=900,
        refresh_ttl_seconds=604800,
    )


@pytest_asyncio.fixture
async def session_factory(
    app_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def app(
    app_engine: AsyncEngine,
    jwt_settings: JwtSettings,
    cleanup_db: None,
    session_factory: async_sessionmaker[AsyncSession],
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


async def _seed_roles_and_users(
    factory: async_sessionmaker[AsyncSession], jwt_settings: JwtSettings
) -> tuple[str, str, str, UUID, UUID]:
    """Seed admin + viewer + analyst on the default team.

    Returns (admin_token, viewer_token, analyst_token, team_id, admin_user_id).
    """
    async with factory() as session:
        admin_role = Role(name="admin", description="full access")
        viewer_role = Role(name="viewer", description="read-only")
        analyst_role = Role(name="analyst", description="analyst")
        team = Team(name="default")
        admin_user = User(
            email="admin@example.com", password_hash=hash_password("adminpw123")
        )
        viewer_user = User(
            email="viewer@example.com", password_hash=hash_password("viewerpw123")
        )
        analyst_user = User(
            email="analyst@example.com", password_hash=hash_password("analystpw123")
        )
        session.add_all(
            [
                admin_role,
                viewer_role,
                analyst_role,
                team,
                admin_user,
                viewer_user,
                analyst_user,
            ]
        )
        await session.flush()

        session.add(
            UserTeamRole(
                user_id=admin_user.id, team_id=team.id, role_id=admin_role.id
            )
        )
        session.add(
            UserTeamRole(
                user_id=viewer_user.id, team_id=team.id, role_id=viewer_role.id
            )
        )
        session.add(
            UserTeamRole(
                user_id=analyst_user.id, team_id=team.id, role_id=analyst_role.id
            )
        )
        await session.commit()

        admin_token = make_access_token(
            admin_user.id, team.id, ("admin",), jwt_settings
        )
        viewer_token = make_access_token(
            viewer_user.id, team.id, ("viewer",), jwt_settings
        )
        analyst_token = make_access_token(
            analyst_user.id, team.id, ("analyst",), jwt_settings
        )

    return admin_token, viewer_token, analyst_token, team.id, admin_user.id


def _make_spec_body(name: str = "sql-analyst") -> dict[str, object]:
    return {
        "name": name,
        "description": "phase 1",
        "system_prompt": "You are a SQL analyst.",
        "output_schema_key": "AnalystResponse",
        "tools": ["run_select", "run_join", "run_aggregate"],
    }


# -------------------- Permission gating --------------------


async def test_viewer_cannot_create_spec(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, viewer, _, _, _ = await _seed_roles_and_users(session_factory, jwt_settings)
    response = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {viewer}"},
        json=_make_spec_body(),
    )
    assert response.status_code == 403


async def test_unauthenticated_create_spec_returns_401(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/agents/specs", json=_make_spec_body())
    assert response.status_code == 401


# -------------------- Happy paths --------------------


async def test_admin_can_create_spec_and_v1(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _ = await _seed_roles_and_users(session_factory, jwt_settings)
    response = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin}"},
        json=_make_spec_body(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "sql-analyst"
    assert body["latest_version"] == 1
    assert UUID(body["id"])

    async with session_factory() as s:
        rows = await s.execute(
            select(AgentVersion).where(AgentVersion.agent_id == UUID(body["id"]))
        )
        versions = rows.scalars().all()
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].output_schema_key == "AnalystResponse"


async def test_duplicate_spec_name_409(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _ = await _seed_roles_and_users(session_factory, jwt_settings)
    body = _make_spec_body()
    r1 = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin}"},
        json=body,
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin}"},
        json=body,
    )
    assert r2.status_code == 409


async def test_admin_can_append_new_version(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _ = await _seed_roles_and_users(session_factory, jwt_settings)
    created = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin}"},
        json=_make_spec_body(),
    )
    spec_id = created.json()["id"]

    v2 = await client.post(
        f"/agents/specs/{spec_id}/versions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "system_prompt": "Updated SQL analyst prompt",
            "output_schema_key": "AnalystResponse",
            "tools": ["run_select"],
        },
    )
    assert v2.status_code == 201, v2.text
    assert v2.json()["version"] == 2

    listing = await client.get(
        f"/agents/specs/{spec_id}/versions",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert listing.status_code == 200
    versions = listing.json()
    assert [v["version"] for v in versions] == [1, 2]


async def test_list_specs_team_scoped(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, viewer, _, team_id, _ = await _seed_roles_and_users(
        session_factory, jwt_settings
    )
    await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin}"},
        json=_make_spec_body(),
    )
    r = await client.get(
        "/agents/specs", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert names == {"sql-analyst"}
    for spec in r.json():
        assert UUID(spec["team_id"]) == team_id


async def test_cross_team_spec_returns_404_not_403(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """A token from team A asking for a spec in team B → 404 (enumeration defense)."""
    admin, _, _, _, _ = await _seed_roles_and_users(session_factory, jwt_settings)
    created = await client.post(
        "/agents/specs",
        headers={"Authorization": f"Bearer {admin}"},
        json=_make_spec_body(),
    )
    spec_id = created.json()["id"]

    # Now create a second team + admin user there, then ask for the first team's spec.
    async with session_factory() as s:
        other_team = Team(name="other")
        other_user = User(
            email="other-admin@example.com",
            password_hash=hash_password("otherpw123"),
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

    response = await client.get(
        f"/agents/specs/{spec_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert response.status_code == 404


async def test_get_unknown_spec_returns_404(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _ = await _seed_roles_and_users(session_factory, jwt_settings)
    response = await client.get(
        f"/agents/specs/{uuid4()}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert response.status_code == 404


# -------------------- INSERT-only role enforcement --------------------


async def test_agent_versions_insert_only_enforced_by_db_role(
    app_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """Create a `pyrene_app` role, run the 0002 REVOKE, then attempt UPDATE
    and DELETE as that role. Both must fail with SQLSTATE 42501.

    This is the canonical INSERT-only proof. The migration's `DO $$` block
    is a no-op when `pyrene_app` doesn't exist, so we create the role
    ourselves to exercise the GRANT/REVOKE path.
    """
    # We need a Spec + Version row to attack.
    async with session_factory() as s:
        team = Team(name="default-insert-only")
        admin_user = User(
            email="insert-only@example.com",
            password_hash=hash_password("pw1234567"),
        )
        s.add_all([team, admin_user])
        await s.flush()
        spec = AgentSpec(
            name="insert-only-test",
            team_id=team.id,
            description="",
            created_by=admin_user.id,
        )
        s.add(spec)
        await s.flush()
        version = AgentVersion(
            agent_id=spec.id,
            version=1,
            output_schema_key="AnalystResponse",
            system_prompt="hi",
            tools=["run_select"],
            created_by=admin_user.id,
        )
        s.add(version)
        await s.commit()
        version_id = version.id

    # Create the pyrene_app role if it doesn't exist, then apply the GRANT/REVOKE
    # from the migration manually (testcontainers starts as superuser).
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                """
                DO $$
                BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pyrene_app') THEN
                    CREATE ROLE pyrene_app LOGIN PASSWORD 'pyrene_app';
                  END IF;
                END $$;
                """
            )
        )
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO pyrene_app"))
        # Reset perms to a known state, then apply migration's REVOKE.
        await conn.execute(
            text("GRANT INSERT, SELECT, UPDATE, DELETE ON agent_versions TO pyrene_app")
        )
        await conn.execute(
            text("REVOKE UPDATE, DELETE ON agent_versions FROM pyrene_app")
        )

    # Now switch role within a transaction and try UPDATE + DELETE.
    async with app_engine.connect() as conn:
        await conn.execute(text("SET ROLE pyrene_app"))
        with pytest.raises(ProgrammingError) as exc_info:
            await conn.execute(
                text("UPDATE agent_versions SET system_prompt = 'evil' WHERE id = :id"),
                {"id": str(version_id)},
            )
        # asyncpg surfaces the SQLSTATE through the wrapped error.
        assert "42501" in str(exc_info.value) or "permission denied" in str(
            exc_info.value
        ).lower()

    async with app_engine.connect() as conn:
        await conn.execute(text("SET ROLE pyrene_app"))
        with pytest.raises(ProgrammingError) as exc_info:
            await conn.execute(
                text("DELETE FROM agent_versions WHERE id = :id"),
                {"id": str(version_id)},
            )
        assert "42501" in str(exc_info.value) or "permission denied" in str(
            exc_info.value
        ).lower()

    # Cleanup: drop the role so the next session-scoped fixture isn't tainted.
    # asyncpg's prepared-statement protocol rejects multi-statement strings,
    # so each command goes through its own `execute()`.
    async with app_engine.begin() as conn:
        await conn.execute(text("REVOKE ALL ON agent_versions FROM pyrene_app"))
        await conn.execute(text("REVOKE USAGE ON SCHEMA public FROM pyrene_app"))
        await conn.execute(text("DROP ROLE IF EXISTS pyrene_app"))
