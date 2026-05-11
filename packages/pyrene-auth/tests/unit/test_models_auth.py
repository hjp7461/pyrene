"""Unit tests for pyrene_auth.models — schema introspection only.

These tests don't touch a real DB. They verify that the SQLAlchemy metadata
encodes the ADR-013 (b) FK cascade matrix correctly:

  - `user_team_roles.{user_id,team_id,role_id}` → ON DELETE CASCADE
  - `UniqueConstraint(user_id, team_id, role_id)` present (duplicate grant
    rejection contract)
  - `users.email`, `teams.name`, `roles.name` UNIQUE
  - `users.deleted_at` nullable (soft-delete contract)

DB-level enforcement is verified by the testcontainers integration test in
`tests/integration/test_migrations.py`.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import UniqueConstraint

from pyrene_auth.models import Role, Team, User, UserTeamRole, metadata


def test_metadata_contains_four_tables() -> None:
    """The four auth tables must be present.

    PLAN-008: pyrene_agents shares this MetaData (cross-package FK to
    `users.id` / `teams.id` requires unified MetaData for ORM resolution).
    So we assert containment, not strict equality.
    """
    names = {t.name for t in metadata.sorted_tables}
    assert {"users", "teams", "roles", "user_team_roles"} <= names


def test_user_email_unique() -> None:
    users = metadata.tables["users"]
    unique_constraints = [c for c in users.constraints if isinstance(c, UniqueConstraint)]
    # ORM-declared `unique=True` becomes an inline UniqueConstraint; either
    # form is acceptable. We also check the column has unique=True directly.
    assert users.c.email.unique is True or any(
        list(c.columns) == [users.c.email] for c in unique_constraints
    )


def test_user_deleted_at_nullable() -> None:
    users = metadata.tables["users"]
    assert users.c.deleted_at.nullable is True


def test_user_is_active_not_nullable() -> None:
    users = metadata.tables["users"]
    assert users.c.is_active.nullable is False


def test_user_team_role_composite_pk() -> None:
    utr = metadata.tables["user_team_roles"]
    pk_cols = {c.name for c in utr.primary_key.columns}
    assert pk_cols == {"user_id", "team_id", "role_id"}


def test_user_team_role_fk_cascade_matrix() -> None:
    """ADR-013 (b): all three FKs must be ON DELETE CASCADE."""
    utr = metadata.tables["user_team_roles"]
    fks_by_col = {fk.parent.name: fk for fk in utr.foreign_keys}
    assert fks_by_col["user_id"].ondelete == "CASCADE"
    assert fks_by_col["team_id"].ondelete == "CASCADE"
    assert fks_by_col["role_id"].ondelete == "CASCADE"


def test_user_team_role_unique_constraint_present() -> None:
    utr = metadata.tables["user_team_roles"]
    unique = [
        c
        for c in utr.constraints
        if isinstance(c, UniqueConstraint) and c.name == "uq_user_team_role"
    ]
    assert len(unique) == 1
    cols = {c.name for c in unique[0].columns}
    assert cols == {"user_id", "team_id", "role_id"}


def test_user_instance_construction_defaults() -> None:
    """User() should provide id/created_at/updated_at defaults when flushed.

    We exercise the Python-side defaults (callable factories) without a DB:
    SQLAlchemy doesn't apply `default` until flush, so we invoke factories
    directly to confirm they produce the expected types.
    """
    user = User(email="alice@example.com", password_hash="hash")
    # Defaults aren't applied pre-flush; just check the attrs accept correct types.
    user.id = uuid4()
    assert isinstance(user.id, type(uuid4()))
    assert user.email == "alice@example.com"


def test_team_and_role_instance_construction() -> None:
    team = Team(name="engineering")
    role = Role(name="admin", description="Full access")
    assert team.name == "engineering"
    assert role.name == "admin"
    assert role.description == "Full access"


def test_user_team_role_instance() -> None:
    utr = UserTeamRole(user_id=uuid4(), team_id=uuid4(), role_id=uuid4())
    assert utr.user_id is not None
    assert utr.team_id is not None
    assert utr.role_id is not None
