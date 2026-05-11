"""End-to-end HTTP integration: /rbac/permissions + /rbac/matrix.

Exercises:
  - admin-only authorization on every endpoint (viewer → 403).
  - POST → resolver cache invalidated → subsequent resolver.can_invoke
    reflects the new row (write-through, ADR-008).
  - GET /rbac/matrix returns the Role x Tool 2D snapshot, including
    roles with zero permissions.
  - DELETE removes a row + invalidates the cache.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool

from pyrene_auth.app import make_app
from pyrene_auth.hashing import hash_password
from pyrene_auth.jwt import JwtSettings, make_access_token
from pyrene_auth.models import Role, Team, User, UserTeamRole
from pyrene_auth.settings import AuthSettings
from pyrene_rbac import (
    PermissionResolver,
    permissions_router,
    reset_resolver,
    set_resolver,
)

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
    """Empties every Phase 2 table the suite touches.

    `permissions` first, then `user_team_roles`, then the auth tables.
    `permissions.role_id` is RESTRICT so roles can only drop AFTER
    permissions are gone — TRUNCATE CASCADE handles this in one shot.
    """
    async with app_engine.begin() as conn:
        await conn.execute(text("SET LOCAL audit.bypass = 'on'"))
        await conn.execute(
            text(
                "TRUNCATE TABLE permissions, user_team_roles, "
                "users, teams, roles RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def jwt_settings() -> JwtSettings:
    return JwtSettings(
        secret="rbac-test-secret-with-thirty-two-plus-bytes-aaaaaaaaa",
        access_ttl_seconds=900,
        refresh_ttl_seconds=604800,
    )


@pytest_asyncio.fixture
async def session_factory(
    app_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )


@pytest_asyncio.fixture
async def resolver() -> AsyncIterator[PermissionResolver]:
    """Shared resolver instance — the write side invalidates it after commit."""
    r = PermissionResolver()
    set_resolver(r)
    try:
        yield r
    finally:
        reset_resolver()


@pytest_asyncio.fixture
async def app(
    app_engine: AsyncEngine,
    jwt_settings: JwtSettings,
    cleanup_db: None,
    session_factory: async_sessionmaker[AsyncSession],
    resolver: PermissionResolver,
) -> FastAPI:
    async def session_dep() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app_instance = make_app(
        auth_settings=AuthSettings(pg_dsn="x", enumeration_defense_ms=10),
        jwt_settings=jwt_settings,
        session_dep=session_dep,
    )
    app_instance.include_router(permissions_router)
    return app_instance


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_admin_viewer_analyst(
    factory: async_sessionmaker[AsyncSession], jwt_settings: JwtSettings
) -> tuple[str, str, str, UUID, UUID, UUID]:
    """Seed three roles + three users, return tokens + role ids.

    Returns:
      (admin_token, viewer_token, analyst_token,
       admin_role_id, viewer_role_id, analyst_role_id)
    """
    async with factory() as session:
        admin_role = Role(name="admin", description="")
        viewer_role = Role(name="viewer", description="")
        analyst_role = Role(name="analyst", description="")
        team = Team(name="default")
        admin_user = User(
            email="admin@example.com", password_hash=hash_password("adminpw123")
        )
        viewer_user = User(
            email="viewer@example.com", password_hash=hash_password("viewerpw123")
        )
        analyst_user = User(
            email="analyst@example.com",
            password_hash=hash_password("analystpw123"),
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

        session.add_all(
            [
                UserTeamRole(
                    user_id=admin_user.id, team_id=team.id, role_id=admin_role.id
                ),
                UserTeamRole(
                    user_id=viewer_user.id, team_id=team.id, role_id=viewer_role.id
                ),
                UserTeamRole(
                    user_id=analyst_user.id,
                    team_id=team.id,
                    role_id=analyst_role.id,
                ),
            ]
        )
        await session.commit()

        return (
            make_access_token(admin_user.id, team.id, ("admin",), jwt_settings),
            make_access_token(viewer_user.id, team.id, ("viewer",), jwt_settings),
            make_access_token(
                analyst_user.id, team.id, ("analyst",), jwt_settings
            ),
            admin_role.id,
            viewer_role.id,
            analyst_role.id,
        )


# -------------------- Forbidden cases --------------------


async def test_viewer_cannot_list_permissions(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, viewer, _, _, _, _ = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    response = await client.get(
        "/rbac/permissions", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert response.status_code == 403


async def test_viewer_cannot_create_permission(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, viewer, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    response = await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {viewer}"},
        json={
            "role_id": str(analyst_role_id),
            "tool_name": "run_select",
            "action": "allow",
        },
    )
    assert response.status_code == 403


async def test_no_auth_header_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/rbac/permissions")
    assert response.status_code == 401


# -------------------- Admin happy paths --------------------


async def test_admin_can_create_and_list_permission(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )

    create = await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "tool_name": "run_select",
            "action": "allow",
        },
    )
    assert create.status_code == 201
    body = create.json()
    assert body["tool_name"] == "run_select"
    assert body["action"] == "allow"

    listing = await client.get(
        "/rbac/permissions", headers={"Authorization": f"Bearer {admin}"}
    )
    assert listing.status_code == 200
    names = [r["tool_name"] for r in listing.json()]
    assert names == ["run_select"]


async def test_create_normalizes_tool_name(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """Case + whitespace input → normalized at write (matches resolver normalization)."""
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )

    create = await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "tool_name": "  Run_Select  ",
            "action": "allow",
        },
    )
    assert create.status_code == 201
    assert create.json()["tool_name"] == "run_select"


async def test_create_duplicate_returns_409(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    body = {
        "role_id": str(analyst_role_id),
        "tool_name": "run_select",
        "action": "allow",
    }
    first = await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json=body,
    )
    assert first.status_code == 201
    second = await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json=body,
    )
    assert second.status_code == 409


async def test_update_flips_action(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    create = await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "tool_name": "run_select",
            "action": "allow",
        },
    )
    pid = create.json()["id"]
    update = await client.put(
        f"/rbac/permissions/{pid}",
        headers={"Authorization": f"Bearer {admin}"},
        json={"action": "deny"},
    )
    assert update.status_code == 200
    assert update.json()["action"] == "deny"


async def test_delete_removes_permission(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    create = await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "tool_name": "run_select",
            "action": "allow",
        },
    )
    pid = create.json()["id"]
    delete = await client.delete(
        f"/rbac/permissions/{pid}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert delete.status_code == 204

    listing = await client.get(
        "/rbac/permissions", headers={"Authorization": f"Bearer {admin}"}
    )
    assert listing.json() == []


# -------------------- Matrix view --------------------


async def test_matrix_includes_all_roles_and_tool_columns(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, viewer_role_id, analyst_role_id = (
        await _seed_admin_viewer_analyst(session_factory, jwt_settings)
    )

    # analyst gets run_select + run_aggregate; viewer gets run_select.
    await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(viewer_role_id),
            "tool_name": "run_select",
            "action": "allow",
        },
    )
    await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "tool_name": "run_select",
            "action": "allow",
        },
    )
    await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "tool_name": "run_aggregate",
            "action": "allow",
        },
    )

    matrix = await client.get(
        "/rbac/matrix", headers={"Authorization": f"Bearer {admin}"}
    )
    assert matrix.status_code == 200
    body = matrix.json()

    tools = body["tools"]
    assert tools == ["run_aggregate", "run_select"]

    # Every role is rendered (admin too, even though it has no rows here).
    by_name = {r["role_name"]: r for r in body["roles"]}
    assert {"admin", "analyst", "viewer"} <= set(by_name.keys())
    assert by_name["analyst"]["tools"] == {
        "run_aggregate": "allow",
        "run_select": "allow",
    }
    assert by_name["viewer"]["tools"] == {"run_select": "allow"}
    assert by_name["admin"]["tools"] == {}


# -------------------- Cache invalidation through CRUD --------------------


async def test_post_invalidates_resolver_cache(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
    resolver: PermissionResolver,
) -> None:
    """A stale `deny` cached → POST allow → next can_invoke returns True."""
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )

    # Warm the cache with the default-deny answer (no permission row yet).
    async with session_factory() as session:
        first = await resolver.can_invoke(
            session,
            role_ids=(analyst_role_id,),
            tool_name="run_select",
        )
        assert first is False
        assert resolver._cache_size() == 1

    # Admin creates the allow row → resolver.invalidate_role kicks in.
    response = await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "tool_name": "run_select",
            "action": "allow",
        },
    )
    assert response.status_code == 201
    assert resolver._cache_size() == 0

    # Fresh lookup reflects the allow row.
    async with session_factory() as session:
        decision = await resolver.can_invoke(
            session,
            role_ids=(analyst_role_id,),
            tool_name="run_select",
        )
        assert decision is True


async def test_delete_invalidates_resolver_cache(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
    resolver: PermissionResolver,
) -> None:
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    create = await client.post(
        "/rbac/permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "tool_name": "run_select",
            "action": "allow",
        },
    )
    pid = create.json()["id"]

    # Warm the cache with the allow.
    async with session_factory() as session:
        assert (
            await resolver.can_invoke(
                session,
                role_ids=(analyst_role_id,),
                tool_name="run_select",
            )
            is True
        )

    delete = await client.delete(
        f"/rbac/permissions/{pid}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert delete.status_code == 204
    assert resolver._cache_size() == 0

    async with session_factory() as session:
        assert (
            await resolver.can_invoke(
                session,
                role_ids=(analyst_role_id,),
                tool_name="run_select",
            )
            is False
        )


# -------------------- Cross-check the unused viewer/analyst tokens --------------------


async def test_analyst_cannot_access_admin_routes(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, _, analyst, _, _, _ = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    response = await client.get(
        "/rbac/permissions", headers={"Authorization": f"Bearer {analyst}"}
    )
    assert response.status_code == 403
