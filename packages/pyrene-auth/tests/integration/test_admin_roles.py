"""Integration tests for /admin/roles + UserTeamRole grant/revoke.

Verifies:
  - viewer / no-role user → 403 on admin endpoints
  - admin user → 200 (create / list / update / delete role)
  - delete on referenced role (FK RESTRICT) → 409 (Phase 2 deferred — no FK
    yet references roles outside `user_team_roles` which CASCADEs, so
    deletion succeeds. RESTRICT-triggered 409 will be exercised by PLAN-010
    once Permission table arrives. For now we assert delete works.)
  - grant_role idempotent + revoke_role 404 on missing grant
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from pyrene_auth.app import make_app
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
                "TRUNCATE TABLE user_team_roles, users, teams, roles "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def jwt_settings() -> JwtSettings:
    return JwtSettings(
        secret="admin-test-secret-with-thirty-two-plus-bytes-aaaaaaaaaaaaa",
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


async def _seed_admin_and_viewer(
    factory: async_sessionmaker[AsyncSession], jwt_settings: JwtSettings
) -> tuple[str, str, UUID]:
    """Insert admin + viewer roles, default team, admin user, viewer user.

    Returns (admin_access_token, viewer_access_token, team_id).
    """
    async with factory() as session:
        admin_role = Role(name="admin", description="full access")
        viewer_role = Role(name="viewer", description="read-only")
        team = Team(name="default")
        admin_user = User(
            email="admin@example.com", password_hash=hash_password("adminpw123")
        )
        viewer_user = User(
            email="viewer@example.com", password_hash=hash_password("viewerpw123")
        )
        session.add_all([admin_role, viewer_role, team, admin_user, viewer_user])
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
        await session.commit()

        admin_token = make_access_token(
            admin_user.id, team.id, ("admin",), jwt_settings
        )
        viewer_token = make_access_token(
            viewer_user.id, team.id, ("viewer",), jwt_settings
        )
        team_id = team.id

    return admin_token, viewer_token, team_id


# -------------------- Forbidden cases --------------------


async def test_viewer_cannot_list_roles(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, viewer, _ = await _seed_admin_and_viewer(session_factory, jwt_settings)
    response = await client.get(
        "/admin/roles", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert response.status_code == 403


async def test_viewer_cannot_create_role(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, viewer, _ = await _seed_admin_and_viewer(session_factory, jwt_settings)
    response = await client.post(
        "/admin/roles",
        headers={"Authorization": f"Bearer {viewer}"},
        json={"name": "analyst", "description": ""},
    )
    assert response.status_code == 403


async def test_no_auth_header_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/roles")
    assert response.status_code == 401


# -------------------- Admin happy paths --------------------


async def test_admin_can_list_roles(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _ = await _seed_admin_and_viewer(session_factory, jwt_settings)
    response = await client.get(
        "/admin/roles", headers={"Authorization": f"Bearer {admin}"}
    )
    assert response.status_code == 200
    names = {r["name"] for r in response.json()}
    assert names == {"admin", "viewer"}


async def test_admin_can_create_role(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _ = await _seed_admin_and_viewer(session_factory, jwt_settings)
    response = await client.post(
        "/admin/roles",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": "analyst", "description": "data analyst"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "analyst"
    assert body["description"] == "data analyst"

    # Verify it landed in DB.
    async with session_factory() as session:
        result = await session.execute(select(Role).where(Role.name == "analyst"))
        role = result.scalar_one()
        assert role.description == "data analyst"


async def test_admin_create_duplicate_role_409(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _ = await _seed_admin_and_viewer(session_factory, jwt_settings)
    response = await client.post(
        "/admin/roles",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": "admin", "description": ""},
    )
    assert response.status_code == 409


async def test_admin_can_update_role_description(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _ = await _seed_admin_and_viewer(session_factory, jwt_settings)
    async with session_factory() as s:
        result = await s.execute(select(Role).where(Role.name == "viewer"))
        viewer_role = result.scalar_one()
        viewer_id = viewer_role.id

    response = await client.put(
        f"/admin/roles/{viewer_id}",
        headers={"Authorization": f"Bearer {admin}"},
        json={"description": "updated"},
    )
    assert response.status_code == 200
    assert response.json()["description"] == "updated"


async def test_admin_can_delete_unused_role(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _ = await _seed_admin_and_viewer(session_factory, jwt_settings)

    # Create a fresh, unused role.
    create = await client.post(
        "/admin/roles",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": "throwaway", "description": ""},
    )
    role_id = create.json()["id"]

    delete = await client.delete(
        f"/admin/roles/{role_id}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert delete.status_code == 204


async def test_delete_role_with_active_grant_cascades(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """Per ADR-013 (b), `user_team_roles.role_id` is CASCADE, so deleting a
    role with active grants succeeds and wipes the grants. The 409 RESTRICT
    behavior fires from `Permission` (PRD-010) tables, not from the m2m.
    """
    admin, _, _ = await _seed_admin_and_viewer(session_factory, jwt_settings)
    async with session_factory() as s:
        result = await s.execute(select(Role).where(Role.name == "viewer"))
        viewer_role = result.scalar_one()
        viewer_id = viewer_role.id

    delete = await client.delete(
        f"/admin/roles/{viewer_id}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert delete.status_code == 204

    # Grants for viewer role should be gone.
    async with session_factory() as s:
        remaining = await s.execute(
            select(UserTeamRole).where(UserTeamRole.role_id == viewer_id)
        )
        assert remaining.scalar_one_or_none() is None


# -------------------- Grant / revoke --------------------


async def test_admin_can_grant_role(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, team_id = await _seed_admin_and_viewer(session_factory, jwt_settings)

    # Create analyst role + a new user.
    await client.post(
        "/admin/roles",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": "analyst", "description": ""},
    )

    async with session_factory() as s:
        result = await s.execute(select(Role).where(Role.name == "analyst"))
        analyst_role = result.scalar_one()
        analyst_id = analyst_role.id
        new_user = User(
            email="newuser@example.com", password_hash=hash_password("pw1234567")
        )
        s.add(new_user)
        await s.commit()
        new_user_id = new_user.id

    response = await client.post(
        f"/admin/users/{new_user_id}/teams/{team_id}/roles/{analyst_id}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert response.status_code == 204

    # Idempotent: second grant is also 204.
    response = await client.post(
        f"/admin/users/{new_user_id}/teams/{team_id}/roles/{analyst_id}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert response.status_code == 204

    async with session_factory() as s:
        result = await s.execute(
            select(UserTeamRole).where(
                UserTeamRole.user_id == new_user_id,
                UserTeamRole.team_id == team_id,
                UserTeamRole.role_id == analyst_id,
            )
        )
        assert result.scalar_one() is not None


async def test_admin_can_revoke_role(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, team_id = await _seed_admin_and_viewer(session_factory, jwt_settings)
    async with session_factory() as s:
        result = await s.execute(select(Role).where(Role.name == "viewer"))
        viewer_role = result.scalar_one()
        viewer_id = viewer_role.id
        result = await s.execute(select(User).where(User.email == "viewer@example.com"))
        viewer_user = result.scalar_one()
        viewer_user_id = viewer_user.id

    response = await client.delete(
        f"/admin/users/{viewer_user_id}/teams/{team_id}/roles/{viewer_id}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert response.status_code == 204


async def test_revoke_missing_grant_returns_404(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, team_id = await _seed_admin_and_viewer(session_factory, jwt_settings)
    from uuid import uuid4

    response = await client.delete(
        f"/admin/users/{uuid4()}/teams/{team_id}/roles/{uuid4()}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert response.status_code == 404


# Suppress unused-import warning for the `cast` helper (kept in case future
# tests need to assert UUID types).
_ = cast
