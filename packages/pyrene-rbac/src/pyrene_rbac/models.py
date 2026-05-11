"""SQLAlchemy 2.x async ORM model for PRD-010 (Tool-level RBAC).

Single table: `permissions` carries a flat Role x Tool x action matrix.
The action column is `Literal["allow", "deny"]`; the resolver applies
`deny > allow` precedence (any matching `deny` row blocks the call —
PRD-010 §2.2 F1 allowlist + explicit override).

FK cascade policy (ADR-013 (b)):
  - `permissions.role_id` -> `roles(id)` ON DELETE **RESTRICT**.
    Implication: deleting a role with live permission rows must first
    revoke the permissions explicitly. PRD-010 §5 calls this out as
    "실수 권한 박탈 방지" — the admin endpoint surfaces a 409 rather
    than silently dropping the privilege bundle (PLAN-007's
    `/admin/roles/{id}` DELETE already proxies the IntegrityError).

Uniqueness:
  - `UNIQUE(role_id, tool_name, action)` — a single (role, tool) pair
    may carry at most one allow row + one deny row (deny wins, so the
    deny row practically dominates). This shape lets PRD-010 §4 YAML
    import express "allow + explicit deny override" without a separate
    schema rev.

Indexes:
  - `(tool_name, role_id)` — hot-path RBAC check `WHERE tool_name = ?
    AND role_id IN (...)`. Order matters: `tool_name` first because
    the gateway always knows the tool before it knows the user (the
    hook receives a `RunContext.tool_name`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Reuse the auth-side MetaData so the `permissions.role_id` FK to `roles(id)`
# resolves at the ORM flush layer. Same pattern as `pyrene_gateway.models`
# (ADR-013 (a) — one Alembic config aggregates every package's metadata).
from pyrene_auth.models import metadata as _shared_metadata


def _now_utc() -> datetime:
    """Module-level default factory (named, not lambda) so SQLAlchemy
    can use it directly without mypy --strict friction on the
    `Mapped[datetime]` inference."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the pyrene-rbac package.

    Shares MetaData with `pyrene_auth.models.Base` so `permissions.role_id`
    -> `roles.id` resolves at the ORM layer. Alembic combines metadata in
    `migrations/env.py` (ADR-013 (a)).
    """

    metadata = _shared_metadata


metadata = Base.metadata


class Permission(Base):
    """One row of the Role x Tool x action matrix (PRD-010 §4).

    `action`:
      - `"allow"` — explicit grant. Default-deny (PRD-010 §2.2 F1) means
        the absence of an `allow` row already blocks; the row is here so
        admins can express "this role can use this tool" affirmatively.
      - `"deny"`  — explicit revocation. Wins over `allow` (PRD-010 §4
        precedence) — a role with both an allow and a deny row is denied.

    `tool_name` is the MCP tool's flat-namespace name (PRD-009 §7 L-02).
    Exact match: case + whitespace preserved at the DB layer; the
    resolver normalizes inputs before lookup (F-02 structured tools).

    Concurrency note: PRD-010 §7 L-01 selects in-memory cache +
    write-through invalidation. Schema-level only the UNIQUE constraint
    on `(role_id, tool_name, action)` guards against duplicate rows.
    """

    __tablename__ = "permissions"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    role_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # ADR-013 (b): RESTRICT so deleting a role with live permissions
        # forces explicit revocation — accidental role drops do not
        # silently strip privileges from every user holding that role.
        ForeignKey("roles.id", ondelete="RESTRICT", name="fk_permissions_role_id"),
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint(
            "role_id", "tool_name", "action", name="uq_permissions_role_tool_action"
        ),
        # Composite index: tool_name leads because RBAC checks filter by
        # `tool_name = ? AND role_id IN (...)`; Postgres uses the leading
        # column's selectivity first.
        Index("ix_permissions_tool_role", "tool_name", "role_id"),
    )


__all__ = ["Base", "Permission", "metadata"]
