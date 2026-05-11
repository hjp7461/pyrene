"""Async engine factories. Two pools, two roles (ADR-013 (d)).

`make_write_engine`    -> uses `PG_DSN`           (role: `pyrene_app`)
`make_readonly_engine` -> uses `PG_READONLY_DSN`  (role: `pyrene_readonly`)

Phase 1 only consumes the readonly engine (run_select). The write engine is
defined here so future Wave 2 work (audit/cost INSERTs, migrations) plugs in
without restructuring.

We intentionally use asyncpg native pooling. PgBouncer transaction mode is
banned by ADR-013 (d) for these connections — `SET ROLE` toggles leak across
pooled connections. asyncpg's pool_size suffices for Phase 1/2 demo loads.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pyrene_sql.settings import Settings

# Pool sizing notes:
#   dev/test: 5 connections is plenty (single worker, function-scoped tests).
#   prod recommendation: tune `pool_size` to ~ (worker_count * 2) and set
#   `max_overflow` according to peak concurrency. Revisit when load testing in Phase 3.
_DEFAULT_POOL_SIZE = 5
_DEFAULT_MAX_OVERFLOW = 5


def _build_engine(dsn: str) -> AsyncEngine:
    return create_async_engine(
        dsn,
        pool_size=_DEFAULT_POOL_SIZE,
        max_overflow=_DEFAULT_MAX_OVERFLOW,
        pool_pre_ping=True,
        future=True,
    )


def make_write_engine(settings: Settings) -> AsyncEngine:
    """Engine bound to `pyrene_app` (writes allowed). Used by migrations / audit / cost."""
    return _build_engine(settings.pg_dsn)


def make_readonly_engine(settings: Settings) -> AsyncEngine:
    """Engine bound to `pyrene_readonly` (DML/DDL revoked at the DB role).

    F-03 second defense: even if the application code forgets to validate, the DB
    rejects writes with `InsufficientPrivilegeError`.
    """
    return _build_engine(settings.pg_readonly_dsn)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
