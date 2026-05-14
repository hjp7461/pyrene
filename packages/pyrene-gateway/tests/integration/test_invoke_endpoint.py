"""HTTP integration: POST /gateway/servers/{id}/tools/{name}/invoke.

PRD-040 / PLAN-040 Wave 1 — exercises the new invoke endpoint with a
fake `StdioMcpClient` (same pattern as `test_discovery_db.py`). Covers:

- AC-1 — viewer (neither admin nor analyst) → 403
- AC-2 — analyst can invoke → 200 + structured result + latency_ms + trace_id
- McpToolError → 422 (사용자 친화 메시지)
- Server not found → 404
- Non-stdio transport → 422
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

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
from pyrene_gateway.mcp_client import McpToolError
from pyrene_gateway.models import MCPServer
from pyrene_gateway.routes.servers import (
    reset_client_factory,
    servers_router,
    set_client_factory,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fakes — duck-typed `StdioMcpClient` substitutes
# ---------------------------------------------------------------------------


class _FakeOkClient:
    """Returns a structured echo response. PRD-040 §2.2 S1 happy path."""

    def __init__(self) -> None:
        self.last_call: tuple[str, dict[str, Any]] | None = None

    async def start(self) -> None:  # pragma: no cover — invoked by factory
        return

    async def stop(self) -> None:
        return

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.last_call = (name, dict(arguments))
        return {"echoed": {"tool": name, "args": arguments}}


class _FakeErrorClient:
    """Raises McpToolError to exercise the 422 mapping."""

    async def start(self) -> None:  # pragma: no cover
        return

    async def stop(self) -> None:
        return

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise McpToolError(f"tool {name!r} blew up")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def cleanup_db(app_engine: AsyncEngine) -> AsyncIterator[None]:
    """Empties tables PRD-040 invoke tests touch.

    `audit.bypass = on` for WORM trigger compatibility (operational-notes).
    """
    async with app_engine.begin() as conn:
        await conn.execute(text("SET LOCAL audit.bypass = 'on'"))
        await conn.execute(
            text(
                "TRUNCATE TABLE mcp_tools, mcp_servers, "
                "user_team_roles, users, teams, roles "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def jwt_settings() -> JwtSettings:
    return JwtSettings(
        secret="invoke-test-secret-32-plus-bytes-aaaaaaaaaaaaaa",
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
async def app(
    app_engine: AsyncEngine,
    jwt_settings: JwtSettings,
    cleanup_db: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    async def session_dep() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    instance = make_app(
        auth_settings=AuthSettings(pg_dsn="x", enumeration_defense_ms=10),
        jwt_settings=jwt_settings,
        session_dep=session_dep,
    )
    instance.include_router(servers_router)
    return instance


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_users_and_server(
    factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
    *,
    transport: str = "stdio",
) -> tuple[str, str, str, MCPServer]:
    """Returns (admin_token, analyst_token, viewer_token, server)."""
    async with factory() as session:
        admin_role = Role(name="admin", description="")
        analyst_role = Role(name="analyst", description="")
        viewer_role = Role(name="viewer", description="")
        team = Team(name="default")
        admin = User(
            email="admin@example.com",
            password_hash=hash_password("adminpw123"),
        )
        analyst = User(
            email="analyst@example.com",
            password_hash=hash_password("analystpw123"),
        )
        viewer = User(
            email="viewer@example.com",
            password_hash=hash_password("viewerpw123"),
        )
        session.add_all(
            [admin_role, analyst_role, viewer_role, team, admin, analyst, viewer]
        )
        await session.flush()
        session.add_all(
            [
                UserTeamRole(
                    user_id=admin.id, team_id=team.id, role_id=admin_role.id
                ),
                UserTeamRole(
                    user_id=analyst.id, team_id=team.id, role_id=analyst_role.id
                ),
                UserTeamRole(
                    user_id=viewer.id, team_id=team.id, role_id=viewer_role.id
                ),
            ]
        )
        server = MCPServer(
            team_id=team.id,
            name="echo",
            transport=transport,
            command="/usr/bin/echo" if transport == "stdio" else None,
            args=["hello"] if transport == "stdio" else [],
            env={},
        )
        session.add(server)
        await session.commit()
        await session.refresh(server)
        return (
            make_access_token(admin.id, team.id, ("admin",), jwt_settings),
            make_access_token(analyst.id, team.id, ("analyst",), jwt_settings),
            make_access_token(viewer.id, team.id, ("viewer",), jwt_settings),
            server,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_invoke_viewer_forbidden(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """AC-1 — viewer (neither admin nor analyst) gets 403."""
    _, _, viewer_token, server = await _seed_users_and_server(
        session_factory, jwt_settings
    )
    response = await client.post(
        f"/gateway/servers/{server.id}/tools/echo/invoke",
        headers={"Authorization": f"Bearer {viewer_token}"},
        json={"arguments": {"text": "hi"}},
    )
    assert response.status_code == 403


async def test_invoke_no_auth_returns_401(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        f"/gateway/servers/{uuid4()}/tools/echo/invoke",
        json={"arguments": {}},
    )
    assert response.status_code == 401


async def test_invoke_analyst_happy_path(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    """AC-2 — analyst gets 200 + structured result + latency_ms + trace_id."""
    _, analyst_token, _, server = await _seed_users_and_server(
        session_factory, jwt_settings
    )
    fake = _FakeOkClient()

    async def factory(_server: MCPServer) -> Any:
        return fake

    set_client_factory(factory)
    try:
        response = await client.post(
            f"/gateway/servers/{server.id}/tools/echo/invoke",
            headers={"Authorization": f"Bearer {analyst_token}"},
            json={"arguments": {"text": "hi"}},
        )
    finally:
        reset_client_factory()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == {"echoed": {"tool": "echo", "args": {"text": "hi"}}}
    assert body["latency_ms"] >= 0.0
    assert isinstance(body["trace_id"], str)  # may be "" outside instrumentation
    assert fake.last_call == ("echo", {"text": "hi"})


async def test_invoke_mcp_tool_error_returns_422(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, analyst_token, _, server = await _seed_users_and_server(
        session_factory, jwt_settings
    )

    async def factory(_server: MCPServer) -> Any:
        return _FakeErrorClient()

    set_client_factory(factory)
    try:
        response = await client.post(
            f"/gateway/servers/{server.id}/tools/boom/invoke",
            headers={"Authorization": f"Bearer {analyst_token}"},
            json={"arguments": {}},
        )
    finally:
        reset_client_factory()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "boom" in detail
    assert "실행 실패" in detail


async def test_invoke_server_not_found_returns_404(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, analyst_token, _, _ = await _seed_users_and_server(
        session_factory, jwt_settings
    )
    response = await client.post(
        f"/gateway/servers/{uuid4()}/tools/echo/invoke",
        headers={"Authorization": f"Bearer {analyst_token}"},
        json={"arguments": {}},
    )
    assert response.status_code == 404


async def test_invoke_non_stdio_transport_returns_422(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    jwt_settings: JwtSettings,
) -> None:
    _, analyst_token, _, server = await _seed_users_and_server(
        session_factory, jwt_settings, transport="sse"
    )
    response = await client.post(
        f"/gateway/servers/{server.id}/tools/echo/invoke",
        headers={"Authorization": f"Bearer {analyst_token}"},
        json={"arguments": {}},
    )
    assert response.status_code == 422
    assert "stdio only" in response.json()["detail"]
