"""Async engine + session factory for pyrene-auth.

Mirrors `pyrene_sql.db` but bound to the auth `PG_DSN` (application role).
Phase 2 only needs the write-capable engine — readonly is `pyrene-sql`'s
concern.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pyrene_auth.settings import AuthSettings

_DEFAULT_POOL_SIZE = 5
_DEFAULT_MAX_OVERFLOW = 5


def make_auth_engine(settings: AuthSettings) -> AsyncEngine:
    return create_async_engine(
        settings.pg_dsn,
        pool_size=_DEFAULT_POOL_SIZE,
        max_overflow=_DEFAULT_MAX_OVERFLOW,
        pool_pre_ping=True,
        future=True,
    )


def make_auth_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


__all__ = ["make_auth_engine", "make_auth_session_factory"]
