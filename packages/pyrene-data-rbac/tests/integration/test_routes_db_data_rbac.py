"""End-to-end HTTP integration: /rbac/data-permissions CRUD.

Exercises:
  - admin-only authorization on every endpoint (viewer → 403).
  - POST → resolver cache invalidated → subsequent
    `resolver.can_access` reflects the new row (write-through, ADR-008).
  - DELETE removes a row + invalidates the cache (1-second invariant).
  - The full wildcard admin grant requires `is_admin_grant=True`.
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
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from pyrene_auth.app import make_app
from pyrene_auth.hashing import hash_password
from pyrene_auth.jwt import JwtSettings, make_access_token
from pyrene_auth.models import Role, Team, User, UserTeamRole
from pyrene_auth.settings import AuthSettings
from pyrene_data_rbac import (
    DataPermissionResolver,
    data_permissions_router,
    reset_resolver,
    set_resolver,
)
from pyrene_data_rbac.permission_resolver import DEFAULT_CONNECTION_ID

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def app_engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def cleanup_db(app_engine: AsyncEngine) -> AsyncIterator[None]:
    """Empties every Phase 2 table the suite touches.

    `data_permissions.role_id` is RESTRICT (ADR-013 (b)); TRUNCATE
    CASCADE handles that in one shot.
    """
    async with app_engine.begin() as conn:
        await conn.execute(text("SET LOCAL audit.bypass = 'on'"))
        await conn.execute(
            text(
                "TRUNCATE TABLE data_permissions, user_team_roles, "
                "users, teams, roles RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def jwt_settings() -> JwtSettings:
    return JwtSettings(
        secret="data-rbac-test-secret-32-plus-bytes-aaaaaaaaaaaaaa",
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
async def resolver() -> AsyncIterator[DataPermissionResolver]:
    r = DataPermissionResolver()
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
    resolver: DataPermissionResolver,
) -> FastAPI:
    async def session_dep() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app_instance = make_app(
        auth_settings=AuthSettings(pg_dsn="x", enumeration_defense_ms=10),
        jwt_settings=jwt_settings,
        session_dep=session_dep,
    )
    app_instance.include_router(data_permissions_router)
    return app_instance


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


async def _seed_admin_viewer_analyst(
    factory: async_sessionmaker[AsyncSession], jwt_settings: JwtSettings
) -> tuple[str, str, str, UUID, UUID, UUID]:
    """Seed three roles + three users, return tokens + role ids."""
    async with factory() as session:
        admin_role = Role(name="admin", description="")
        viewer_role = Role(name="viewer", description="")
        analyst_role = Role(name="analyst", description="")
        team = Team(name="default")
        admin_user = User(
            email="admin@example.com",
            password_hash=hash_password("adminpw123"),
        )
        viewer_user = User(
            email="viewer@example.com",
            password_hash=hash_password("viewerpw123"),
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
                    user_id=admin_user.id,
                    team_id=team.id,
                    role_id=admin_role.id,
                ),
                UserTeamRole(
                    user_id=viewer_user.id,
                    team_id=team.id,
                    role_id=viewer_role.id,
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
            make_access_token(
                admin_user.id, team.id, ("admin",), jwt_settings
            ),
            make_access_token(
                viewer_user.id, team.id, ("viewer",), jwt_settings
            ),
            make_access_token(
                analyst_user.id, team.id, ("analyst",), jwt_settings
            ),
            admin_role.id,
            viewer_role.id,
            analyst_role.id,
        )


# -------------------- Forbidden cases --------------------


async def test_viewer_cannot_list(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, viewer, _, _, _, _ = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    response = await client.get(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert response.status_code == 403


async def test_no_auth_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/rbac/data-permissions")
    assert response.status_code == 401


# -------------------- Admin happy paths --------------------


async def test_admin_can_create_and_list(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    create = await client.post(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "connection_id": str(DEFAULT_CONNECTION_ID),
            "schema": "public",
            "table": "payment",
            "action": "allow",
        },
    )
    assert create.status_code == 201
    body = create.json()
    assert body["schema"] == "public"
    assert body["table"] == "payment"
    assert body["action"] == "allow"

    listing = await client.get(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["table"] == "payment"


async def test_create_normalizes_quoted_uppercase(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """PM amend bypass cases — quoted / uppercase identifiers normalize."""
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    create = await client.post(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "connection_id": str(DEFAULT_CONNECTION_ID),
            "schema": '"PUBLIC"',
            "table": '"PAYMENT"',
            "action": "allow",
        },
    )
    assert create.status_code == 201
    body = create.json()
    assert body["schema"] == "public"
    assert body["table"] == "payment"


async def test_create_full_wildcard_without_admin_flag_422(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """PRD-011 §위험 #3 — full wildcard requires explicit ack."""
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    create = await client.post(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "connection_id": str(DEFAULT_CONNECTION_ID),
            "schema": "*",
            "table": "*",
            "action": "allow",
        },
    )
    assert create.status_code == 422


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
        "connection_id": str(DEFAULT_CONNECTION_ID),
        "schema": "public",
        "table": "payment",
        "action": "allow",
    }
    first = await client.post(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json=body,
    )
    assert first.status_code == 201
    second = await client.post(
        "/rbac/data-permissions",
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
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "connection_id": str(DEFAULT_CONNECTION_ID),
            "schema": "public",
            "table": "payment",
            "action": "allow",
        },
    )
    pid = create.json()["id"]
    update = await client.put(
        f"/rbac/data-permissions/{pid}",
        headers={"Authorization": f"Bearer {admin}"},
        json={"action": "deny"},
    )
    assert update.status_code == 200
    assert update.json()["action"] == "deny"


async def test_delete_removes_row(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    create = await client.post(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "connection_id": str(DEFAULT_CONNECTION_ID),
            "schema": "public",
            "table": "payment",
            "action": "allow",
        },
    )
    pid = create.json()["id"]
    delete = await client.delete(
        f"/rbac/data-permissions/{pid}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert delete.status_code == 204

    listing = await client.get(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert listing.json() == []


# -------------------- Cache invalidation through CRUD --------------------


async def test_post_invalidates_resolver_cache(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
    resolver: DataPermissionResolver,
) -> None:
    """A stale `deny` cached → POST allow → next can_access returns True.

    Verifies PRD-011 §S3 + §6 — matrix invalidation 1 초 이내 반영.
    """
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )

    # Warm the cache with the default-deny answer.
    async with session_factory() as session:
        first = await resolver.can_access(
            session,
            role_ids=(analyst_role_id,),
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table="payment",
        )
        assert first is False
        assert resolver._cache_size() == 1

    # Admin creates the allow row → resolver.invalidate_role kicks in.
    response = await client.post(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "connection_id": str(DEFAULT_CONNECTION_ID),
            "schema": "public",
            "table": "payment",
            "action": "allow",
        },
    )
    assert response.status_code == 201
    assert resolver._cache_size() == 0

    # Fresh lookup reflects the allow row.
    async with session_factory() as session:
        decision = await resolver.can_access(
            session,
            role_ids=(analyst_role_id,),
            connection_id=DEFAULT_CONNECTION_ID,
            schema="public",
            table="payment",
        )
        assert decision is True


async def test_delete_invalidates_resolver_cache(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
    resolver: DataPermissionResolver,
) -> None:
    admin, _, _, _, _, analyst_role_id = await _seed_admin_viewer_analyst(
        session_factory, jwt_settings
    )
    create = await client.post(
        "/rbac/data-permissions",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "role_id": str(analyst_role_id),
            "connection_id": str(DEFAULT_CONNECTION_ID),
            "schema": "public",
            "table": "payment",
            "action": "allow",
        },
    )
    pid = create.json()["id"]

    async with session_factory() as session:
        assert (
            await resolver.can_access(
                session,
                role_ids=(analyst_role_id,),
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table="payment",
            )
            is True
        )

    delete = await client.delete(
        f"/rbac/data-permissions/{pid}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert delete.status_code == 204
    assert resolver._cache_size() == 0

    async with session_factory() as session:
        assert (
            await resolver.can_access(
                session,
                role_ids=(analyst_role_id,),
                connection_id=DEFAULT_CONNECTION_ID,
                schema="public",
                table="payment",
            )
            is False
        )
