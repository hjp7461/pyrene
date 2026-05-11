"""Unit tests for `AgentSpec` / `AgentVersion` SQLAlchemy models.

Validates the ORM class shape (column types, FK matrices, unique
constraints) without a live DB. Live FK behaviour (CASCADE / RESTRICT)
is exercised in `tests/integration/test_specs_api.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pyrene_agents.models import AgentSpec, AgentVersion, metadata


def test_metadata_contains_agent_tables() -> None:
    """`metadata` is shared with pyrene_auth — both packages' tables visible."""
    names = set(metadata.tables.keys())
    assert "agent_specs" in names
    assert "agent_versions" in names
    # Auth side present too (shared MetaData — see models.py).
    assert "users" in names
    assert "teams" in names


def test_agent_specs_unique_constraint_team_name() -> None:
    table = metadata.tables["agent_specs"]
    uniques = {tuple(c.name for c in u.columns) for u in table.constraints if hasattr(u, "columns")}
    # The (team_id, name) tuple must appear in some constraint.
    assert ("team_id", "name") in uniques or ("name", "team_id") in uniques


def test_agent_specs_fk_cascade_matrix() -> None:
    """team_id → CASCADE, created_by → RESTRICT (ADR-013 (b))."""
    table = metadata.tables["agent_specs"]
    fks = {fk.parent.name: fk for fk in table.foreign_keys}
    assert "team_id" in fks
    assert "created_by" in fks
    assert fks["team_id"].ondelete == "CASCADE"
    assert fks["created_by"].ondelete == "RESTRICT"


def test_agent_versions_fk_cascade_matrix() -> None:
    """agent_id → CASCADE, created_by → RESTRICT (ADR-013 (b))."""
    table = metadata.tables["agent_versions"]
    fks = {fk.parent.name: fk for fk in table.foreign_keys}
    assert fks["agent_id"].ondelete == "CASCADE"
    assert fks["created_by"].ondelete == "RESTRICT"


def test_agent_versions_unique_agent_version() -> None:
    table = metadata.tables["agent_versions"]
    uniques = {tuple(c.name for c in u.columns) for u in table.constraints if hasattr(u, "columns")}
    assert ("agent_id", "version") in uniques or ("version", "agent_id") in uniques


def test_agent_versions_table_marker_insert_only() -> None:
    """The `info={"insert_only": True}` marker documents the DB role policy."""
    table = metadata.tables["agent_versions"]
    assert table.info.get("insert_only") is True


def test_agent_spec_instance_construction() -> None:
    """Pure ORM construction should accept all required fields without DB."""
    team_id = uuid4()
    creator = uuid4()
    spec = AgentSpec(
        name="sql-analyst",
        team_id=team_id,
        description="phase 1",
        created_by=creator,
    )
    assert spec.name == "sql-analyst"
    assert spec.team_id == team_id
    assert spec.created_by == creator
    # `description` default is empty string when omitted.
    spec2 = AgentSpec(name="x", team_id=team_id, created_by=creator)
    assert spec2.description == "" or spec2.description is None


def test_agent_version_instance_construction() -> None:
    agent_id = uuid4()
    creator = uuid4()
    version = AgentVersion(
        agent_id=agent_id,
        version=1,
        output_schema_key="AnalystResponse",
        system_prompt="hi",
        tools=["run_select"],
        created_by=creator,
    )
    assert version.agent_id == agent_id
    assert version.version == 1
    assert version.output_schema_key == "AnalystResponse"
    assert version.tools == ["run_select"]
    assert version.published_at is None
    # `created_at` is server-defaulted in the DB; on a detached instance the
    # ORM doesn't auto-fill it before flush, so we don't assert it here.
    _ = datetime.now(UTC)


def test_agent_specs_team_id_fk_target() -> None:
    """FK string spec must point at `teams.id` (the auth-side table)."""
    table = metadata.tables["agent_specs"]
    team_fk = next(
        (fk for fk in table.foreign_keys if fk.parent.name == "team_id"), None
    )
    assert team_fk is not None
    # `_colspec` carries the original string ("teams.id"); we deliberately
    # avoid `.column.table.name` because resolving the FK requires loading
    # the `pyrene_auth.models` metadata, which we don't want in this unit
    # test.
    assert "teams.id" in str(team_fk.target_fullname)


def test_agent_specs_created_by_fk_target() -> None:
    """FK string spec must point at `users.id`."""
    table = metadata.tables["agent_specs"]
    fk = next(
        (fk for fk in table.foreign_keys if fk.parent.name == "created_by"), None
    )
    assert fk is not None
    assert "users.id" in str(fk.target_fullname)


def test_agent_versions_created_by_fk_target() -> None:
    """FK string spec must point at `users.id` (ADR-013 b RESTRICT)."""
    table = metadata.tables["agent_versions"]
    fk = next(
        (fk for fk in table.foreign_keys if fk.parent.name == "created_by"), None
    )
    assert fk is not None
    assert "users.id" in str(fk.target_fullname)
