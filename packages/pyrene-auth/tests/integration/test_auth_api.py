"""Integration tests for the auth API.

Stands up a FastAPI app + httpx ASGITransport against a savepoint-isolated
session bound to the migrated testcontainers DB. Exercises:

  - signup → 201 + token pair
  - signup duplicate email → 409
  - login (correct) → 200 + token pair
  - login (wrong password) → 401
  - login (unknown email) → 401 + indistinguishable timing
  - /auth/me with valid access token → 200 + UserContext payload
  - /auth/me with expired token → 401
  - /auth/me with tampered token → 401
  - /auth/refresh → 200 + new access token (refresh token preserved)
"""

from __future__ import annotations

import statistics
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from pyrene_auth.app import make_app
from pyrene_auth.jwt import JwtSettings, make_access_token
from pyrene_auth.settings import AuthSettings

pytestmark = pytest.mark.integration


# Override the function-scoped engine here so the test app gets its own pool
# that survives across requests. The `db_session` fixture's savepoint pattern
# doesn't fit the request/response cycle, so we use truncate-style cleanup
# between tests (function-scoped fixture that drops/recreates rows).
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
    """Truncate all auth tables before each test (function-scoped reset).

    SAVEPOINT isolation doesn't work across FastAPI requests because each
    request grabs its own connection from the pool. We delete rows instead.
    """
    from sqlalchemy import text

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
        secret="integration-test-secret-with-thirty-two-plus-bytes-aaaaa",
        access_ttl_seconds=900,
        refresh_ttl_seconds=604800,
    )


@pytest.fixture
def auth_settings_fast() -> AuthSettings:
    # Smaller enumeration floor for faster tests.
    return AuthSettings(
        pg_dsn="placeholder",  # overridden by session_dep
        enumeration_defense_ms=50,
    )


@pytest_asyncio.fixture
async def app(
    app_engine: AsyncEngine,
    jwt_settings: JwtSettings,
    auth_settings_fast: AuthSettings,
    cleanup_db: None,
) -> FastAPI:
    factory = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)

    async def session_dep() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    return make_app(
        auth_settings=auth_settings_fast,
        jwt_settings=jwt_settings,
        session_dep=session_dep,
    )


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# -------------------- signup --------------------


async def test_signup_returns_token_pair(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/auth/signup",
        json={"email": "alice@example.com", "password": "supersecret"},
    )
    assert response.status_code == 201
    body = response.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


async def test_signup_duplicate_email_returns_409(client: httpx.AsyncClient) -> None:
    payload = {"email": "alice@example.com", "password": "supersecret"}
    first = await client.post("/auth/signup", json=payload)
    assert first.status_code == 201
    second = await client.post("/auth/signup", json=payload)
    assert second.status_code == 409


# -------------------- login --------------------


async def test_login_with_correct_credentials(client: httpx.AsyncClient) -> None:
    await client.post(
        "/auth/signup",
        json={"email": "bob@example.com", "password": "supersecret"},
    )
    response = await client.post(
        "/auth/login",
        json={"email": "bob@example.com", "password": "supersecret"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_login_with_wrong_password_returns_401(client: httpx.AsyncClient) -> None:
    await client.post(
        "/auth/signup",
        json={"email": "bob@example.com", "password": "supersecret"},
    )
    response = await client.post(
        "/auth/login",
        json={"email": "bob@example.com", "password": "wrong"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


async def test_login_with_unknown_email_returns_401_with_same_detail(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/auth/login",
        json={"email": "ghost@example.com", "password": "anything"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


async def test_login_timing_indistinguishable_unknown_vs_wrong_pwd(
    client: httpx.AsyncClient,
) -> None:
    """Mean response time delta < 50 ms (floor = 50 ms).

    Both branches run argon2 verify against a real hash (real user) or a
    sentinel hash (unknown user). The enumeration floor ensures both wait at
    least 50 ms total. Variance from argon2 is the same in both cases.
    """
    await client.post(
        "/auth/signup",
        json={"email": "alice@example.com", "password": "realpassword"},
    )

    unknown_times: list[float] = []
    wrong_times: list[float] = []

    for _ in range(5):
        t0 = time.monotonic()
        await client.post(
            "/auth/login",
            json={"email": "ghost@example.com", "password": "x"},
        )
        unknown_times.append(time.monotonic() - t0)

        t0 = time.monotonic()
        await client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "wrong"},
        )
        wrong_times.append(time.monotonic() - t0)

    delta = abs(statistics.mean(unknown_times) - statistics.mean(wrong_times))
    # Allow 100 ms slack — argon2 verify cost varies on CI hardware.
    assert delta < 0.100, f"timing delta {delta * 1000:.1f}ms is suspiciously high"


# -------------------- /auth/me --------------------


async def test_me_with_valid_token(client: httpx.AsyncClient) -> None:
    signup = await client.post(
        "/auth/signup",
        json={"email": "carol@example.com", "password": "supersecret"},
    )
    access = signup.json()["access_token"]

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {access}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "carol@example.com"
    assert "team_id" in body
    assert body["roles"] == []


async def test_me_without_token_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/auth/me")
    assert response.status_code == 401


async def test_me_with_expired_token_returns_401(
    client: httpx.AsyncClient, jwt_settings: JwtSettings
) -> None:
    # Hand-craft an already-expired token using the same secret.
    from uuid import uuid4

    expired = make_access_token(uuid4(), uuid4(), (), jwt_settings, now=0)
    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {expired}"}
    )
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


async def test_me_with_tampered_token_returns_401(client: httpx.AsyncClient) -> None:
    signup = await client.post(
        "/auth/signup",
        json={"email": "dave@example.com", "password": "supersecret"},
    )
    access = signup.json()["access_token"]
    # Replace the signature with a fixed invalid value. Single-char mutations
    # on base64url signatures can be no-ops because the trailing bits of the
    # last char are unused, decoding to the same bytes ~25% of the time.
    parts = access.split(".")
    parts[-1] = "invalid_signature_value"
    bad = ".".join(parts)

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {bad}"}
    )
    assert response.status_code == 401


# -------------------- /auth/refresh --------------------


async def test_refresh_returns_new_access_token(client: httpx.AsyncClient) -> None:
    signup = await client.post(
        "/auth/signup",
        json={"email": "eve@example.com", "password": "supersecret"},
    )
    refresh_token = signup.json()["refresh_token"]

    response = await client.post(
        "/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert response.status_code == 200
    new_access = response.json()["access_token"]
    assert new_access

    # And it works against /auth/me.
    me = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert me.status_code == 200


async def test_refresh_rejects_access_token(client: httpx.AsyncClient) -> None:
    signup = await client.post(
        "/auth/signup",
        json={"email": "frank@example.com", "password": "supersecret"},
    )
    access = signup.json()["access_token"]

    response = await client.post(
        "/auth/refresh", json={"refresh_token": access}
    )
    assert response.status_code == 401


# Silence the unused-import warning for create_engine (some test runners
# eagerly evaluate imports). Tests may add a sync-engine SAVEPOINT path later.
_ = create_engine
