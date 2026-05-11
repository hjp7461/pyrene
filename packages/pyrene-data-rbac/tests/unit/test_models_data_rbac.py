"""DataPermission model basics — column types, defaults, ORM identity.

These are pure-Python checks against the SQLAlchemy declarative class.
Round-trip and FK enforcement live under integration tests because
they need a real Postgres engine.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import inspect

from pyrene_data_rbac import DataPermission, metadata


def test_data_permission_columns_present() -> None:
    """All PRD-011 §4 columns are present + typed sensibly."""
    cols = {c.name: c for c in inspect(DataPermission).columns}
    assert set(cols) == {
        "id",
        "role_id",
        "connection_id",
        "schema",
        "table",
        "action",
        "created_at",
    }
    # action storage type — Literal mapped to String(8) in the DB.
    assert cols["action"].type.length == 8  # type: ignore[attr-defined]
    # role_id is the FK to roles(id); type is UUID.
    assert cols["role_id"].nullable is False
    assert cols["connection_id"].nullable is False
    assert cols["schema"].nullable is False
    assert cols["table"].nullable is False


def test_role_id_fk_restrict() -> None:
    """ADR-013 (b): role_id FK is RESTRICT on delete."""
    role_id_col = inspect(DataPermission).columns["role_id"]
    fks = list(role_id_col.foreign_keys)
    assert len(fks) == 1, "exactly one FK on role_id"
    assert fks[0].ondelete == "RESTRICT", "ADR-013 (b) — RESTRICT, not CASCADE"


def test_python_table_attribute_named_table_name() -> None:
    """`table` is a SQL keyword; the Python attribute is `table_name` so
    the SQLAlchemy declarative parses cleanly. The DB column stays
    `"table"` (matching PRD-011 §4 + `pyrene_schema_embeddings`)."""
    py_attr = inspect(DataPermission).attrs.get("table_name")
    assert py_attr is not None
    db_cols = {c.name for c in inspect(DataPermission).columns}
    assert "table" in db_cols


def test_unique_constraint_is_five_tuple() -> None:
    """UNIQUE on (role_id, connection_id, schema, "table", action)."""
    uniques = [
        c
        for c in metadata.tables["data_permissions"].constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert len(uniques) == 1
    cols = [c.name for c in uniques[0].columns]  # type: ignore[attr-defined]
    assert cols == ["role_id", "connection_id", "schema", "table", "action"]


def test_metadata_table_registered() -> None:
    """data_permissions is registered on the shared MetaData so the
    Alembic env.py picks it up via `combine_metadata(...)`."""
    assert "data_permissions" in metadata.tables


def test_default_id_factory_returns_uuid() -> None:
    """`id` defaults to uuid4 — non-trivial because mypy --strict often
    complains about Mapped[UUID] defaults."""
    perm = DataPermission(
        role_id=uuid4(),
        connection_id=uuid4(),
        schema="public",
        table_name="payment",
        action="allow",
    )
    # SQLAlchemy hasn't run the default yet (no flush); cross-check
    # the registered Column default callable resolves to uuid4.
    id_default = inspect(DataPermission).columns["id"].default
    assert id_default is not None
    value = id_default.arg(None)  # type: ignore[attr-defined]
    assert isinstance(value, UUID)
    # `perm` itself is unflushed but the field assignment doesn't blow up.
    assert perm.role_id is not None
