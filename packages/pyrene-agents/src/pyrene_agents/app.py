"""FastAPI application factory for pyrene-agents.

Wires the auth routers (re-exported from pyrene-auth so the agent registry
is usable standalone in integration tests) and the agent registry routers.
The host service typically composes these into its own FastAPI instance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pyrene_agents.routes import run_router, specs_router
from pyrene_auth.db import make_auth_engine, make_auth_session_factory
from pyrene_auth.dependencies import (
    set_jwt_settings_dependency,
    set_session_dependency,
)
from pyrene_auth.jwt import JwtSettings
from pyrene_auth.routes.admin.roles import admin_router
from pyrene_auth.routes.auth import auth_router
from pyrene_auth.settings import AuthSettings


def make_app(
    auth_settings: AuthSettings | None = None,
    jwt_settings: JwtSettings | None = None,
    session_dep: Callable[..., Any] | None = None,
    engine: AsyncEngine | None = None,
) -> FastAPI:
    """Build a FastAPI instance bound to auth + agents routers.

    Mirrors `pyrene_auth.app.make_app` so the wiring of dependency overrides
    is consistent across packages.
    """
    settings = auth_settings or AuthSettings()
    jwt_cfg = jwt_settings or JwtSettings()

    if session_dep is None:
        eng = engine or make_auth_engine(settings)
        factory = make_auth_session_factory(eng)

        async def _default_session_dep() -> AsyncIterator[AsyncSession]:
            async with factory() as session:
                yield session

        session_dep = _default_session_dep

    set_session_dependency(session_dep)
    set_jwt_settings_dependency(lambda: jwt_cfg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(title="pyrene-agents", lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(specs_router)
    app.include_router(run_router)
    return app


__all__ = ["make_app"]
