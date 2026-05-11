"""Schema-level tests for the `permissions` SQLAlchemy model.

These tests do not hit a database — they inspect the MetaData
declarations so issues like a missing UNIQUE or a wrong FK ondelete
surface in the unit lane.
"""

from __future__ import annotations

from sqlalchemy import inspect

from pyrene_rbac.models import Permission, metadata


def test_metadata_contains_permissions_table() -> None:
    assert "permissions" in metadata.tables


def test_permissions_unique_constraint_present() -> None:
    table = metadata.tables["permissions"]
    unique_names = {c.name for c in table.constraints if c.name is not None}
    assert "uq_permissions_role_tool_action" in unique_names


def test_permissions_composite_index_present() -> None:
    table = metadata.tables["permissions"]
    index_names = {i.name for i in table.indexes}
    assert "ix_permissions_tool_role" in index_names

    # Tool name MUST be the leading column (matches RBAC WHERE clause).
    target = next(i for i in table.indexes if i.name == "ix_permissions_tool_role")
    col_names = [c.name for c in target.columns]
    assert col_names == ["tool_name", "role_id"]


def test_permissions_role_id_fk_restrict() -> None:
    """ADR-013 (b): RESTRICT keeps roles pinned in place while permissions exist."""
    table = metadata.tables["permissions"]
    role_id_col = table.c.role_id
    fks = list(role_id_col.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "roles"
    assert fk.ondelete == "RESTRICT"


def test_permission_instance_construction() -> None:
    """Bare attribute round-trip (no DB)."""
    import uuid

    role_id = uuid.uuid4()
    perm = Permission(role_id=role_id, tool_name="run_select", action="allow")
    assert perm.role_id == role_id
    assert perm.tool_name == "run_select"
    assert perm.action == "allow"


def test_inspector_columns_present() -> None:
    """Defense-in-depth — `inspect()` should see every Mapped column."""
    mapper = inspect(Permission)
    names = {c.key for c in mapper.columns}
    assert names == {"id", "role_id", "tool_name", "action", "created_at"}
