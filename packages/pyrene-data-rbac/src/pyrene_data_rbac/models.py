"""SQLAlchemy 2.x async ORM model for PRD-011 (Data-level RBAC).

Single table: `data_permissions` carries a flat
Role x Connection x Schema x Table x action matrix. `action` is
`Literal["allow", "deny"]`; the resolver applies `deny > allow`
precedence (any matching `deny` row blocks the access — PRD-011 §F1).

FK cascade policy (ADR-013 (b)):
  - `data_permissions.role_id` → `roles(id)` ON DELETE **RESTRICT**.
    Implication: deleting a role with live data-permission rows must
    first revoke the rows explicitly. PRD-011 §위험 #3 / PLAN-011 §위험 #3
    call this out as "실수 권한 박탈 방지".
  - `connection_id` is intentionally NOT a FK at Phase 2 — the
    `connections` table is owned by PLAN-011 Day 1 in some
    formulations; here we keep the column as a plain UUID so the
    package can ship independent of a `connections` table. PLAN-011
    Day 1 (this slice) ships the column; a follow-up wave promotes it
    to a FK once the connection registry materializes.

Uniqueness:
  - `UNIQUE(role_id, connection_id, schema, "table", action)` — a
    single `(role, connection, schema, table)` quadruple may carry at
    most one allow row + one deny row.

Wildcards:
  - `schema` and `"table"` accept the literal value `"*"` meaning
    "every schema" / "every table on the matched schema". `(*, *)` is
    therefore an admin-equivalent grant on a given connection; the
    schema validator forces the caller to pass `is_admin_grant=True`
    when the request body would create that row (PRD-011 위험 #3 +
    PM amend — wildcard precedence carries a hard warning).

Indexes:
  - `(role_id, connection_id, schema, "table")` — hot-path lookup
    `WHERE role_id IN (...) AND connection_id = ? AND ...`.
  - `(connection_id, schema, "table")` — bulk listing per-connection
    for the admin UI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Reuse the auth-side MetaData so the `data_permissions.role_id` FK to
# `roles(id)` resolves at the ORM flush layer. Same pattern as
# `pyrene_rbac.models`. ADR-013 (a) — one Alembic config aggregates
# every package's metadata in `migrations/env.py`.
from pyrene_auth.models import metadata as _shared_metadata


def _now_utc() -> datetime:
    """Module-level default factory (named, not lambda) so SQLAlchemy
    can use it directly without mypy --strict friction on the
    `Mapped[datetime]` inference."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the pyrene-data-rbac package.

    Shares MetaData with `pyrene_auth.models.Base` so the
    `data_permissions.role_id` → `roles.id` FK resolves at the ORM
    layer. Alembic combines metadata in `migrations/env.py` (ADR-013 (a)).
    """

    metadata = _shared_metadata


metadata = Base.metadata


class DataPermission(Base):
    """One row of the Role x Connection x Schema x Table x action matrix.

    `action`:
      - `"allow"` — explicit grant. Default-deny (PRD-011 §F1) means
        the absence of an `allow` row already blocks; the row is here
        so admins can express "this role can read this table" plainly.
      - `"deny"`  — explicit revocation. Wins over `allow` (PRD-011 §4
        precedence): a role with both an allow and a deny on the same
        `(connection, schema, table)` is denied.

    Wildcards:
      - `schema == "*"` matches every schema on the row's connection.
      - `"table" == "*"` matches every table on the matched schema(s).
      - The resolver applies explicit > wildcard within the deny-wins
        envelope (see `permission_resolver.py`).
    """

    __tablename__ = "data_permissions"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    role_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # ADR-013 (b): RESTRICT so deleting a role with live data
        # permissions forces explicit revocation — accidental role
        # drops do not silently strip read access for every user
        # holding that role.
        ForeignKey(
            "roles.id", ondelete="RESTRICT", name="fk_data_permissions_role_id"
        ),
        nullable=False,
    )
    # Phase 2 ships `connection_id` as a plain UUID (no FK) — the
    # `connections` table lives in a downstream PLAN slice. The column
    # is non-null so every row scopes to exactly one connection. PLAN
    # follow-up promotes to a FK with `ON DELETE RESTRICT`.
    connection_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    # Schema names are arbitrary identifiers (or the wildcard "*"); 128
    # chars is well over the Postgres `NAMEDATALEN` ceiling of 63 with
    # a generous margin for quoted / unicode identifiers.
    schema: Mapped[str] = mapped_column(String(128), nullable=False)
    # `table` is a SQL keyword so we keep the quoted column name on the
    # DB side via the `Text` storage type (matching `pyrene_schema_embeddings`)
    # and use the attribute name `table_name` on the Python side. PRD-011
    # §4 spells the field as `table`, so the Pydantic schema also exposes
    # it as `table` on the wire.
    table_name: Mapped[str] = mapped_column(
        "table", Text, nullable=False
    )
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint(
            "role_id",
            "connection_id",
            "schema",
            "table",
            "action",
            name="uq_data_permissions_role_conn_schema_table_action",
        ),
        # Hot-path RBAC lookup: filter by (role IN (...), connection_id,
        # schema, table). role_id leads because the WHERE clause has the
        # IN clause on it (high selectivity once the connection scope is
        # applied via the second column).
        Index(
            "ix_data_permissions_role_conn_schema_table",
            "role_id",
            "connection_id",
            "schema",
            "table",
        ),
        # Admin UI listing: every row on a connection.
        Index(
            "ix_data_permissions_conn_schema_table",
            "connection_id",
            "schema",
            "table",
        ),
    )


__all__ = ["Base", "DataPermission", "metadata"]
