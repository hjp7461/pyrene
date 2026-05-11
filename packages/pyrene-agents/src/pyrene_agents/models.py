"""SQLAlchemy 2.x async ORM models for PRD-008 (AgentSpec + AgentVersion).

Schema overview:
  - `agent_specs`: top-level agent identity (team-scoped, mutable description).
  - `agent_versions`: immutable history of (system_prompt, tools, output_schema_key)
    triples. New versions are appended; UPDATE/DELETE is forbidden via DB
    role privileges (see migration 0002_agent_registry).

FK cascade policy (ADR-013 (b)):
  - `agent_specs.team_id` → `teams(id)` ON DELETE CASCADE
    (specs are scoped to a team; team closure cleans up the spec history).
  - `agent_specs.created_by` → `users(id)` ON DELETE RESTRICT
    (preserves authorship; users must be soft-deleted, not hard-deleted).
  - `agent_versions.agent_id` → `agent_specs(id)` ON DELETE CASCADE
    (versions live and die with their parent spec).
  - `agent_versions.created_by` → `users(id)` ON DELETE RESTRICT
    (PRD-008 §3.2 — agent author tracking preserved across soft-delete).

INSERT-only role policy:
  `agent_versions` is treated as an append-only audit log. The migration
  REVOKEs UPDATE and DELETE from `pyrene_app`. The Python ORM `__table_args__`
  carries `{"info": {"insert_only": True}}` as a documentation marker; the
  actual enforcement is at the DB role layer (defense-in-depth, F-03 spirit).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Reuse the auth-side MetaData so FK targets (`users.id`, `teams.id`) resolve
# at ORM flush time. ADR-013 (a) allows packages to keep distinct Bases as
# long as the *MetaData* lookup is unified — Alembic combines metadata in
# `migrations/env.py`, but SQLAlchemy's per-table FK resolver needs the
# target table object in the same MetaData instance as the source. Importing
# `pyrene_auth.models.metadata` and binding it here is the standard fix.
from pyrene_auth.models import metadata as _shared_metadata


def _now_utc() -> datetime:
    """Module-level default factory (matches `pyrene_auth.models._now_utc`)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the agents package.

    Shares MetaData with `pyrene_auth.models.Base` so cross-package FKs
    (`agent_specs.team_id → teams.id`, `...created_by → users.id`) resolve
    at the ORM layer. Alembic still sees a single combined MetaData in
    `migrations/env.py` (ADR-013 (a)).
    """

    metadata = _shared_metadata


metadata = Base.metadata


class AgentSpec(Base):
    """Team-scoped agent identity.

    Mutable fields (`description`) are PATCH-able by admins. The (name,
    team_id) tuple is unique — two teams can each own a "sql-analyst" spec.
    """

    __tablename__ = "agent_specs"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_agent_specs_team_id"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    created_by: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # ADR-013 (b): RESTRICT — preserve authorship across user lifecycle.
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_agent_specs_created_by"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_agent_specs_team_name"),
    )


class AgentVersion(Base):
    """Immutable agent version (system_prompt + tools + output_schema_key).

    INSERT-only: new versions are appended (`version = max(version)+1`).
    UPDATE / DELETE are blocked at the DB role layer (see migration).

    `tools` is `ARRAY(TEXT)` — ordered list of tool names that the builder
    resolves via `ToolRegistry`. Names that are not registered are rejected
    by the builder (Day 2).

    `output_schema_key` is constrained to `OutputSchemaKey` Literal values at
    the Pydantic schema layer. The DB stores the string verbatim; mismatch
    becomes a builder ValidationError at run time.
    """

    __tablename__ = "agent_versions"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    agent_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "agent_specs.id", ondelete="CASCADE", name="fk_agent_versions_agent_id"
        ),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    output_schema_key: Mapped[str] = mapped_column(String(128), nullable=False)
    system_prompt: Mapped[str] = mapped_column(String(16384), nullable=False)
    tools: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), nullable=False, default=list
    )
    created_by: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # ADR-013 (b): RESTRICT — PRD-008 §3.2 agent author retention.
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_agent_versions_created_by"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_version"),
        # INSERT-only marker — documentation for the migration; the GRANT/REVOKE
        # in 0002_agent_registry.py is the enforcement.
        {"info": {"insert_only": True}},
    )


__all__ = ["AgentSpec", "AgentVersion", "Base", "metadata"]
