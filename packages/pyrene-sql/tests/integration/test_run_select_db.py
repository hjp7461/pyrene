"""Integration tests for run_select against a real Postgres + DVD Rental.

Covers the F-03 second defense (DB role rejects writes) by attempting DDL/DML
through the same read-only connection used for SELECTs.
"""

from __future__ import annotations

import pytest
from asyncpg.exceptions import InsufficientPrivilegeError  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_sql.tools.run_select import RunSelectInput, run_select_direct

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_select_normal(readonly_session: AsyncSession) -> None:
    inp = RunSelectInput(table="public.category", columns=["name"], limit=5)
    out = await run_select_direct(readonly_session, inp)
    assert out.row_count == 5
    assert out.truncated is True  # category has 16 rows
    assert all("name" in row for row in out.rows)


async def test_select_payment_aggregate_via_where(
    readonly_session: AsyncSession,
) -> None:
    """Demo scenario adjacent to PRD-001 §2.1 — payment table accessible."""
    inp = RunSelectInput(
        table="public.payment",
        columns=["amount"],
        where="amount >= :min_amount",
        where_params={"min_amount": 9.0},
        limit=10,
    )
    out = await run_select_direct(readonly_session, inp)
    assert out.row_count <= 10
    assert all(float(row["amount"]) >= 9.0 for row in out.rows)


async def test_truncation_flag_false_when_under_limit(
    readonly_session: AsyncSession,
) -> None:
    inp = RunSelectInput(table="public.category", columns="*", limit=100)
    out = await run_select_direct(readonly_session, inp)
    assert out.row_count == 16
    assert out.truncated is False


def _is_privilege_error(exc: BaseException) -> bool:
    """A privilege violation may surface as asyncpg's InsufficientPrivilegeError
    directly, or wrapped one or two layers deep by SQLAlchemy. asyncpg's
    PostgresError subclasses preserve the SQLSTATE on `.sqlstate`; class
    `42501` is "insufficient_privilege" (the canonical signal we care about).
    """
    if isinstance(exc, InsufficientPrivilegeError):
        return True
    orig = getattr(exc, "orig", None)
    if isinstance(orig, InsufficientPrivilegeError):
        return True
    # Fall back to SQLSTATE on whatever DBAPI exception bubbled up.
    sqlstate = getattr(orig, "sqlstate", None) or getattr(exc, "sqlstate", None)
    return sqlstate == "42501"


@pytest.mark.parametrize(
    "label,statement",
    [
        ("ddl_create_table", "CREATE TABLE pyrene_test_foo (id int)"),
        ("dml_insert", "INSERT INTO category (name) VALUES ('hacked')"),
        ("dml_update", "UPDATE category SET name = 'hacked'"),
        ("dml_delete", "DELETE FROM category"),
        ("ddl_drop", "DROP TABLE category"),
        ("dml_truncate", "TRUNCATE TABLE category"),
    ],
)
async def test_write_rejected_by_db_role(
    readonly_session: AsyncSession, label: str, statement: str
) -> None:
    """F-03 dual defense: even if the application failed to validate, the
    `pyrene_readonly` Postgres role rejects writes."""
    with pytest.raises((DBAPIError, ProgrammingError)) as excinfo:
        await readonly_session.execute(text(statement))
    assert _is_privilege_error(excinfo.value), (
        f"[{label}] expected InsufficientPrivilegeError, got "
        f"{type(excinfo.value).__name__}: {excinfo.value!r}"
    )


async def test_copy_from_rejected(readonly_session: AsyncSession) -> None:
    """COPY ... FROM is a write; the readonly role must not be able to issue it.

    Postgres may surface either InsufficientPrivilegeError (privilege) or a
    permission error before the file is touched. We accept any DBAPIError as
    long as the COPY does not succeed."""
    with pytest.raises(DBAPIError):
        await readonly_session.execute(
            text("COPY category FROM '/tmp/does_not_exist.csv'")
        )
