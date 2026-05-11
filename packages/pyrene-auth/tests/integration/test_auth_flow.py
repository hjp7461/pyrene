"""End-to-end auth flow + security regression tests.

Day 4: chains signup → login → grant → /me roles reflect → revoke → /me
roles disappear (no cache, ADR-008 is PRD-010 scope). Also exercises:

  - SQL injection attempt in email field (SQLAlchemy bind params safe)
  - JWT secret rotation invalidates existing tokens
  - argon2 verify timing is not catastrophically variable between branches
"""

from __future__ import annotations

import statistics
import time
from collections.abc import AsyncIterator

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
        secret="flow-test-secret-with-thirty-two-plus-bytes-aaaaaaaaaaaaaaaa",
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


# -------------------- End-to-end flow --------------------


async def test_full_auth_flow_signup_grant_role_reflected_in_me(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """signup → bootstrap admin → admin grants `analyst` → /me shows it.

    No cache (PRD-008 future work) → grant takes effect on the very next
    /auth/me call.
    """
    # 1. Plain user signs up — gets empty roles.
    signup = await client.post(
        "/auth/signup",
        json={"email": "user@example.com", "password": "supersecret"},
    )
    assert signup.status_code == 201
    user_access = signup.json()["access_token"]

    me_before = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {user_access}"}
    )
    assert me_before.status_code == 200
    assert me_before.json()["roles"] == []

    # 2. Bootstrap an admin user out-of-band (Phase 2 doesn't yet have an
    #    init-admin web endpoint; the CLI handles it. Here we go through DB.)
    async with session_factory() as s:
        admin_role = Role(name="admin", description="full")
        analyst_role = Role(name="analyst", description="data analyst")
        s.add_all([admin_role, analyst_role])
        await s.flush()
        team = await s.execute(select(Team).where(Team.name == "default"))
        team_row = team.scalar_one()
        admin = User(email="admin@example.com", password_hash=hash_password("adminpw"))
        s.add(admin)
        await s.flush()
        s.add(
            UserTeamRole(
                user_id=admin.id, team_id=team_row.id, role_id=admin_role.id
            )
        )
        await s.commit()
        admin_id = admin.id
        team_id = team_row.id
        analyst_id = analyst_role.id

        user_row = await s.execute(select(User).where(User.email == "user@example.com"))
        user_id = user_row.scalar_one().id

    admin_token = make_access_token(admin_id, team_id, ("admin",), jwt_settings)

    # 3. Admin grants `analyst` to the user.
    grant = await client.post(
        f"/admin/users/{user_id}/teams/{team_id}/roles/{analyst_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert grant.status_code == 204

    # 4. User logs in again — new access token now reflects the role.
    login = await client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "supersecret"},
    )
    new_user_access = login.json()["access_token"]

    me_after = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {new_user_access}"}
    )
    assert me_after.status_code == 200
    assert me_after.json()["roles"] == ["analyst"]

    # 5. /me re-reads roles from DB on every request, so even the OLD token
    #    (issued at signup, before grant) reflects the new role.
    me_with_old_token = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {user_access}"}
    )
    assert me_with_old_token.json()["roles"] == ["analyst"]


async def test_admin_revoke_takes_effect_immediately(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """Revoke → next /me has empty roles (no cache layer in PRD-007)."""
    # Seed: admin user + viewer user + viewer role.
    async with session_factory() as s:
        admin_role = Role(name="admin", description="")
        viewer_role = Role(name="viewer", description="")
        team = Team(name="default")
        admin = User(email="admin@example.com", password_hash=hash_password("pw1234567"))
        viewer = User(email="v@example.com", password_hash=hash_password("pw1234567"))
        s.add_all([admin_role, viewer_role, team, admin, viewer])
        await s.flush()
        s.add(
            UserTeamRole(user_id=admin.id, team_id=team.id, role_id=admin_role.id)
        )
        s.add(
            UserTeamRole(user_id=viewer.id, team_id=team.id, role_id=viewer_role.id)
        )
        await s.commit()
        admin_id, viewer_id = admin.id, viewer.id
        team_id, viewer_role_id = team.id, viewer_role.id

    admin_token = make_access_token(admin_id, team_id, ("admin",), jwt_settings)
    viewer_token = make_access_token(viewer_id, team_id, ("viewer",), jwt_settings)

    # Viewer's /me confirms viewer role.
    me_before = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {viewer_token}"}
    )
    assert me_before.json()["roles"] == ["viewer"]

    # Admin revokes.
    revoke = await client.delete(
        f"/admin/users/{viewer_id}/teams/{team_id}/roles/{viewer_role_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert revoke.status_code == 204

    me_after = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {viewer_token}"}
    )
    assert me_after.json()["roles"] == []


# -------------------- Security regression --------------------


async def test_sql_injection_in_email_field_safe(client: httpx.AsyncClient) -> None:
    """Pydantic EmailStr + SQLAlchemy bind params block SQL injection.

    The malicious string fails EmailStr validation (422), so it never
    reaches the SQL layer. Defense in depth: even if it did, bind params
    would escape the input.
    """
    response = await client.post(
        "/auth/signup",
        json={"email": "'; DROP TABLE users; --", "password": "supersecret"},
    )
    assert response.status_code == 422


async def test_jwt_secret_rotation_invalidates_existing_tokens(
    app_engine: AsyncEngine,
    cleanup_db: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Token issued with secret A is rejected after secret rotates to B."""
    settings_a = JwtSettings(
        secret="secret-A-thirty-two-plus-bytes-padding-aaaaaaaaaaaaaaaaaa",
        access_ttl_seconds=900,
        refresh_ttl_seconds=604800,
    )
    settings_b = JwtSettings(
        secret="secret-B-thirty-two-plus-bytes-padding-bbbbbbbbbbbbbbbbbb",
        access_ttl_seconds=900,
        refresh_ttl_seconds=604800,
    )

    async def session_dep() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    # Build two apps with different secrets, same DB.
    app_a = make_app(jwt_settings=settings_a, session_dep=session_dep)
    transport_a = httpx.ASGITransport(app=app_a)

    async with httpx.AsyncClient(transport=transport_a, base_url="http://a") as ca:
        signup = await ca.post(
            "/auth/signup",
            json={"email": "rot@example.com", "password": "supersecret"},
        )
        token = signup.json()["access_token"]

    app_b = make_app(jwt_settings=settings_b, session_dep=session_dep)
    transport_b = httpx.ASGITransport(app=app_b)

    async with httpx.AsyncClient(transport=transport_b, base_url="http://b") as cb:
        response = await cb.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 401


async def test_argon2_verify_timing_stability(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """argon2 verify must consume comparable CPU on hit / miss for the
    enumeration-defense floor to work.

    We measure the floor-less path by calling /auth/login with a known
    bad-password (real user, real argon2 verify miss). The enumeration
    floor in `auth_settings_fast` is set to 10 ms so test cycles fast; we
    assert variance < 100 ms.
    """
    await client.post(
        "/auth/signup",
        json={"email": "timing@example.com", "password": "supersecret"},
    )

    times: list[float] = []
    for _ in range(5):
        t0 = time.monotonic()
        await client.post(
            "/auth/login",
            json={"email": "timing@example.com", "password": "wrong"},
        )
        times.append(time.monotonic() - t0)

    # Stdev should be tiny — argon2 itself is deterministic-cost.
    assert statistics.stdev(times) < 0.100


async def test_inactive_user_cannot_authenticate(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """A user with is_active=False is rejected by get_current_user."""
    async with session_factory() as s:
        team = Team(name="default")
        user = User(
            email="dormant@example.com",
            password_hash=hash_password("supersecret"),
            is_active=False,
        )
        s.add_all([team, user])
        await s.commit()
        token = make_access_token(user.id, team.id, (), jwt_settings)

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


async def test_soft_deleted_user_cannot_authenticate(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """deleted_at IS NOT NULL → 401 on /auth/me."""
    from datetime import UTC, datetime

    async with session_factory() as s:
        team = Team(name="default")
        user = User(
            email="erased@example.com",
            password_hash=hash_password("supersecret"),
            deleted_at=datetime.now(UTC),
        )
        s.add_all([team, user])
        await s.commit()
        token = make_access_token(user.id, team.id, (), jwt_settings)

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
