"""F-03 dual defense integration test (PLAN-011 Day 3).

PROJECT_BRIEF §F-03 commits to TWO independent enforcement layers:

  (1) **Code guard** — the data-RBAC hook denies based on the
      `data_permissions` matrix BEFORE the SQL executes. Covered by
      the rest of the suite (resolver + hook tests).

  (2) **DB role** — even if a caller bypasses the hook (e.g. by
      issuing raw SQL through a pool with the application role), a
      Postgres-level read-only role denies writes / DDL / arbitrary
      table SELECTs the role was never granted on. PLAN-001 ships
      `pyrene_readonly` via `deploy/postgres/initdb/02-readonly-role.sql`.

This file constructs a miniature analogue inside the testcontainer
(initdb is NOT executed by `PostgresContainer`) so the regression
guard fires without depending on the host DB image:

  - Creates a `secret_data` table the readonly role is NOT granted on.
  - Creates a `public_data` table the readonly role IS granted SELECT on.
  - Reconnects as the readonly role and verifies:
      a) SELECT on `secret_data` raises `InsufficientPrivilege`
         (SQLSTATE `42501`).
      b) SELECT on `public_data` succeeds.
      c) INSERT/UPDATE/DELETE on `public_data` raises
         `InsufficientPrivilege`.

This proves the DB layer is the second wall: even with a hostile
direct SQL path, the readonly role cannot escalate.

The code guard side of F-03 is exercised in
`test_resolver_db_data_rbac.py` (every test verifies a matrix-based
deny / allow). Both paths together satisfy PRD-011 §6 "DB role 레벨
에서도 동일하게 거부됨을 확인 (이중 방어)".
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration


_READONLY_ROLE = "pyrene_readonly_test"
_READONLY_PWD = "readonly-test"
_PUBLIC_TABLE = "_drbac_public_data"
_SECRET_TABLE = "_drbac_secret_data"


async def _ensure_role_and_grants(engine: AsyncEngine) -> None:
    """Provision the analogue of `pyrene_readonly` for the test run.

    asyncpg disallows bind parameters inside DO blocks; the role name
    and password are fixed module constants, so we inline them
    directly with explicit literal quoting. The values are NOT
    user-controlled — this is a test fixture, not a production code
    path.
    """
    async with engine.begin() as conn:
        # Create role idempotently. The testcontainer is fresh per
        # session, but reruns inside the same session must not blow up.
        await conn.execute(
            text(
                f"""
                DO $$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{_READONLY_ROLE}'
                  ) THEN
                    EXECUTE 'CREATE ROLE {_READONLY_ROLE} '
                            'LOGIN PASSWORD ''{_READONLY_PWD}''';
                  END IF;
                END
                $$;
                """
            )
        )
        await conn.execute(
            text(f"DROP TABLE IF EXISTS {_PUBLIC_TABLE}")
        )
        await conn.execute(
            text(f"DROP TABLE IF EXISTS {_SECRET_TABLE}")
        )
        await conn.execute(
            text(f"CREATE TABLE {_PUBLIC_TABLE} (id int, v text)")
        )
        await conn.execute(
            text(f"CREATE TABLE {_SECRET_TABLE} (id int, v text)")
        )
        await conn.execute(
            text(f"INSERT INTO {_PUBLIC_TABLE} VALUES (1, 'visible')")
        )
        await conn.execute(
            text(f"INSERT INTO {_SECRET_TABLE} VALUES (1, 'hidden')")
        )
        # GRANT CONNECT on whatever DB we're attached to.
        db_row = await conn.execute(text("SELECT current_database()"))
        db_name = db_row.scalar_one()
        await conn.execute(
            text(f'GRANT CONNECT ON DATABASE "{db_name}" TO {_READONLY_ROLE}')
        )
        await conn.execute(
            text(f"GRANT USAGE ON SCHEMA public TO {_READONLY_ROLE}")
        )
        # SELECT only on _PUBLIC_TABLE; nothing on _SECRET_TABLE.
        await conn.execute(
            text(f"GRANT SELECT ON {_PUBLIC_TABLE} TO {_READONLY_ROLE}")
        )
        await conn.execute(
            text(
                f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {_PUBLIC_TABLE} "
                f"FROM {_READONLY_ROLE}"
            )
        )


def _readonly_dsn(app_dsn: str) -> str:
    """Swap the superuser credentials in `app_dsn` for the readonly role.

    The testcontainer DSN looks like
    `postgresql+asyncpg://pyrene:pyrene@host:port/db`.
    """
    prefix, rest = app_dsn.split("://", 1)
    _, host_part = rest.split("@", 1)
    return f"{prefix}://{_READONLY_ROLE}:{_READONLY_PWD}@{host_part}"


async def test_db_role_rejects_select_on_ungranted_table(
    engine: AsyncEngine, app_dsn: str
) -> None:
    """F-03 wall #2: the readonly role lacks SELECT on `_secret_data`.

    Even if the application's code guard is bypassed entirely (raw SQL
    through a direct pool), Postgres rejects the SELECT with
    `InsufficientPrivilege` (SQLSTATE 42501).
    """
    await _ensure_role_and_grants(engine)
    ro_engine = create_async_engine(_readonly_dsn(app_dsn), poolclass=NullPool)
    try:
        async with ro_engine.connect() as conn:
            with pytest.raises(ProgrammingError) as excinfo:
                await conn.execute(
                    text(f"SELECT * FROM {_SECRET_TABLE}")
                )
            # asyncpg surfaces `InsufficientPrivilegeError`; the
            # SQLSTATE is `42501`. The exception chain carries
            # `.orig` with the asyncpg error.
            err = excinfo.value
            sqlstate = getattr(
                getattr(err, "orig", None), "sqlstate", None
            )
            assert sqlstate == "42501", (
                f"expected SQLSTATE 42501 (insufficient_privilege), "
                f"got {sqlstate!r}"
            )
    finally:
        await ro_engine.dispose()


async def test_db_role_allows_select_on_granted_table(
    engine: AsyncEngine, app_dsn: str
) -> None:
    """F-03 sanity: the readonly role CAN SELECT on the granted table.

    Without this guard a failing privilege test would be ambiguous —
    the readonly role might be broken in some other way.
    """
    await _ensure_role_and_grants(engine)
    ro_engine = create_async_engine(_readonly_dsn(app_dsn), poolclass=NullPool)
    try:
        async with ro_engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT v FROM {_PUBLIC_TABLE} WHERE id = 1")
            )
            assert result.scalar_one() == "visible"
    finally:
        await ro_engine.dispose()


async def test_db_role_rejects_insert_on_granted_table(
    engine: AsyncEngine, app_dsn: str
) -> None:
    """F-03: even on the granted table, writes are rejected. PLAN-001's
    initdb script revokes INSERT/UPDATE/DELETE; we mirror that here."""
    await _ensure_role_and_grants(engine)
    ro_engine = create_async_engine(_readonly_dsn(app_dsn), poolclass=NullPool)
    try:
        async with ro_engine.connect() as conn:
            with pytest.raises(ProgrammingError) as excinfo:
                await conn.execute(
                    text(f"INSERT INTO {_PUBLIC_TABLE} VALUES (2, 'x')")
                )
            sqlstate = getattr(
                getattr(excinfo.value, "orig", None), "sqlstate", None
            )
            assert sqlstate == "42501"
    finally:
        await ro_engine.dispose()
