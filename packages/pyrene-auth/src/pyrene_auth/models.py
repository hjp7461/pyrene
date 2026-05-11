"""SQLAlchemy 2.x async ORM models for PRD-007 (User / Team / Role + UserTeamRole).

FK cascade policy (ADR-013 (b)):
  - `UserTeamRole.user_id` / `.team_id` / `.role_id` → ON DELETE CASCADE
    (membership is ephemeral; user soft-delete or team closure cleans up
    membership rows automatically).
  - User soft-delete only (`users.deleted_at`); hard delete forbidden because
    audit / cost / agent_versions FK targets `users(id)` with RESTRICT
    (PLAN-008 / PLAN-013 / PLAN-015).

Schema constraints:
  - `users.email` UNIQUE
  - `teams.name` UNIQUE
  - `roles.name` UNIQUE
  - `UserTeamRole` composite PK (user_id, team_id, role_id) — duplicate grants
    rejected at the DB layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now_utc() -> datetime:
    """Module-level default factory so SQLAlchemy can use it without a lambda.

    Using a named function (rather than `default=lambda: datetime.now(UTC)`)
    keeps mypy --strict happy with the Mapped[datetime] inference.
    """
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Shared declarative base. `Base.metadata` is what Alembic targets."""


metadata = Base.metadata


class User(Base):
    """Authentication subject.

    Soft delete only: `deleted_at IS NOT NULL` excludes the user from
    `get_current_user` lookups but leaves audit/cost FKs intact (ADR-013 (b)).
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now_utc,
        onupdate=_now_utc,
    )


class Team(Base):
    """Tenant boundary. Phase 2 multi-team users join via `UserTeamRole`."""

    __tablename__ = "teams"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )


class Role(Base):
    """Named permission bundle ("admin", "analyst", "viewer", ...).

    PRD-007 §7 L-03: role is a DB row (not enum) so PRD-010 can introduce
    new roles dynamically.
    """

    __tablename__ = "roles"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")


class UserTeamRole(Base):
    """Team-scoped role membership (PRD-007 §7 L-02).

    Composite PK + UniqueConstraint redundancy: the unique constraint is
    informational for tooling; the composite PK is the actual enforcement.
    All three FKs are CASCADE so membership cleans up with its parents.
    """

    __tablename__ = "user_team_roles"

    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    team_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("user_id", "team_id", "role_id", name="uq_user_team_role"),
    )
