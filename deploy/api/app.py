"""Unified FastAPI entrypoint for the `pyrene-api` container.

PLAN-018 Day 1 §1 — single ASGI app that composes every Phase 2 backend
package into one process. Each package keeps its own router module; this
file is intentionally thin so that the import surface mirrors what an
integrator would assemble in their own host service.

Routers wired (in BRIEF §6 order):

  auth      → /auth/*           (PRD-007)
  agents    → /agents/*         (PRD-008)
  gateway   → /servers/*        (PRD-009)
  rbac      → /permissions/*    (PRD-010)
  data-rbac → /data-permissions (PRD-011)
  metering  → /metering/*       (PRD-013)
  budget    → /budgets/*        (PRD-014)
  audit     → /audit/*          (PRD-015)

A `/health` endpoint is added at module level — it answers with a static
JSON body so the docker-compose healthcheck does not need a database
connection. The dependency-override wiring follows the same pattern as
`pyrene_auth.app.make_app`: a default session dependency is installed
from `AuthSettings()` (which reads `PG_DSN`).

This file is not yet part of any package — it is only imported by the
container at runtime. Unit tests for each router live in the owning
package; an integration smoke that hits `/health` is intentionally out of
scope for PLAN-018 (the dispatch is documentation/demo only).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.db import make_auth_engine, make_auth_session_factory
from pyrene_auth.dependencies import (
    set_jwt_settings_dependency,
    set_session_dependency,
)
from pyrene_auth.jwt import JwtSettings
from pyrene_auth.routes.admin.roles import admin_router as auth_admin_router
from pyrene_auth.routes.auth import auth_router
from pyrene_auth.settings import AuthSettings


def _build() -> FastAPI:
    settings = AuthSettings()
    jwt_cfg = JwtSettings()
    engine = make_auth_engine(settings)
    factory = make_auth_session_factory(engine)

    async def _session_dep() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    set_session_dependency(_session_dep)
    set_jwt_settings_dependency(lambda: jwt_cfg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="pyrene-api", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Phase 2 routers (each package owns wiring of its own dependencies via
    # `set_*` hooks; here we only register the routers themselves so they
    # are reachable from the container).
    app.include_router(auth_router)
    app.include_router(auth_admin_router)

    # The following imports are deferred because some packages register
    # exception handlers / dependency setters at import time and we want
    # them to attach to *this* app instance.
    from pyrene_agents.routes import run_router, specs_router

    app.include_router(specs_router)
    app.include_router(run_router)

    from pyrene_audit.routes import audit_router
    from pyrene_budget.routes import budgets_router, register_exception_handlers
    from pyrene_data_rbac.routes import data_permissions_router
    from pyrene_gateway.routes import servers_router
    from pyrene_metering.routes import usage_router
    from pyrene_rbac.routes import permissions_router

    app.include_router(audit_router)
    app.include_router(budgets_router)
    app.include_router(data_permissions_router)
    app.include_router(servers_router)
    app.include_router(usage_router)
    app.include_router(permissions_router)
    register_exception_handlers(app)

    return app


app = _build()
