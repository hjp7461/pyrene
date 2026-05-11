"""0007 data permissions: data_permissions + pyrene_schema_embeddings.connection_id.

Revision ID: 0007_data_permissions
Revises: 0006_audit_log
Create Date: 2026-05-11

PLAN-011 Day 1 (Wave 8). Owns two changes on a single revision:

1. **`data_permissions` table** — fresh CREATE TABLE (no ALTER needed).
   FK `role_id` → `roles(id)` ON DELETE RESTRICT (ADR-013 (b)).

2. **`pyrene_schema_embeddings.connection_id`** — ADD COLUMN 3-step
   pattern from ADR-013 (c). The initdb script
   `deploy/postgres/initdb/03-schema-embeddings.sql` already ships the
   column with a default for fresh Phase 1 boots, so the migration is
   idempotent against both paths:

     - Step A: ADD COLUMN connection_id UUID NULL  (skipped if present)
     - Step B: UPDATE rows still NULL to the Phase 1 sentinel UUID
     - Step C: ALTER COLUMN connection_id SET NOT NULL
              + add UNIQUE(connection_id, schema, "table") if missing

   HNSW is graph-based (pgvector PLAN-002 chose HNSW over IVFFlat for
   precisely this reason); adding a non-indexed column requires NO
   REINDEX. Total reindex cost: 0.

ADR cross-references:
  - ADR-013 (b) FK cascade matrix — RESTRICT on `role_id`.
  - ADR-013 (c) online ALTER 3-step — ADD COLUMN NULL → backfill →
    SET NOT NULL.
  - ADR-007 — row / column masking deferred to Phase 1.5. Protection
    unit is `(connection, schema, table)`.

### Wave 8 chain reality

PLAN-011 (this migration, 0007), PLAN-012, PLAN-014 are landing in
parallel. The PLAN-014 budget migration also reserves a number; the
PM coordinator linearizes the chain. Until then, this migration
declares `down_revision = 0006_audit_log` so isolated feature branches
upgrade cleanly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007_data_permissions"
down_revision: str | None = "0006_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Phase 1 single-connection default. Must match
# `pyrene_sql.schema.models.DEFAULT_CONNECTION_ID` and
# `pyrene_data_rbac.permission_resolver.DEFAULT_CONNECTION_ID`.
_PHASE1_CONNECTION_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # (1) data_permissions — fresh CREATE TABLE
    # ------------------------------------------------------------------
    op.create_table(
        "data_permissions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "roles.id",
                # ADR-013 (b): RESTRICT — accidental role drop must
                # not silently strip data privileges from every user
                # holding that role. PRD-011 §위험 #3.
                ondelete="RESTRICT",
                name="fk_data_permissions_role_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("schema", sa.String(length=128), nullable=False),
        sa.Column("table", sa.Text, nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "role_id",
            "connection_id",
            "schema",
            "table",
            "action",
            name="uq_data_permissions_role_conn_schema_table_action",
        ),
    )
    # Hot-path lookup: (role_id IN ..., connection_id, schema, table).
    op.create_index(
        "ix_data_permissions_role_conn_schema_table",
        "data_permissions",
        ["role_id", "connection_id", "schema", "table"],
    )
    # Admin UI listing: every row on a connection.
    op.create_index(
        "ix_data_permissions_conn_schema_table",
        "data_permissions",
        ["connection_id", "schema", "table"],
    )

    # ------------------------------------------------------------------
    # (2) pyrene_schema_embeddings.connection_id — ADD COLUMN 3-step
    #     (ADR-013 (c)). Idempotent against initdb-bootstrapped DBs
    #     where the column already exists.
    # ------------------------------------------------------------------
    bind = op.get_bind()
    embeddings_exists = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'pyrene_schema_embeddings'"
        )
    ).scalar()
    if not embeddings_exists:
        # Test environments managed entirely via Alembic don't ship the
        # initdb SQL; create the minimal table so the 3-step pattern
        # has something to operate on. We deliberately keep this
        # branch lean — the production schema (HNSW index, pgvector
        # column) is still owned by initdb. The migration only
        # guarantees the columns the data-RBAC layer relies on.
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        op.execute(
            """
            CREATE TABLE pyrene_schema_embeddings (
              id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              schema       TEXT NOT NULL,
              "table"      TEXT NOT NULL,
              description  TEXT NOT NULL,
              updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

    # Step A — ADD COLUMN NULL (idempotent — `IF NOT EXISTS` keeps the
    # statement a no-op against initdb-bootstrapped DBs).
    op.execute(
        "ALTER TABLE pyrene_schema_embeddings "
        "ADD COLUMN IF NOT EXISTS connection_id UUID NULL;"
    )

    # Step B — backfill rows still NULL. Cast the bind param to UUID
    # so asyncpg does not infer VARCHAR (DatatypeMismatchError).
    op.execute(
        sa.text(
            "UPDATE pyrene_schema_embeddings "
            "SET connection_id = CAST(:cid AS uuid) "
            "WHERE connection_id IS NULL"
        ).bindparams(cid=_PHASE1_CONNECTION_ID)
    )

    # Step C — SET NOT NULL. Idempotent because Postgres accepts
    # re-issuing the constraint on an already-NOT-NULL column.
    op.execute(
        "ALTER TABLE pyrene_schema_embeddings "
        "ALTER COLUMN connection_id SET NOT NULL;"
    )

    # PLAN-002 retriever filters by connection_id + (schema, table)
    # tuple. The initdb script already adds the unique constraint
    # under the name `pyrene_schema_embeddings_unique_target`; add it
    # only when absent (Alembic-only test envs).
    has_unique = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'pyrene_schema_embeddings_unique_target'"
        )
    ).scalar()
    if not has_unique:
        op.execute(
            "ALTER TABLE pyrene_schema_embeddings "
            "ADD CONSTRAINT pyrene_schema_embeddings_unique_target "
            'UNIQUE (connection_id, schema, "table");'
        )


def downgrade() -> None:
    # data_permissions first (FK on role_id; no dependents inside this
    # revision).
    op.drop_index(
        "ix_data_permissions_conn_schema_table",
        table_name="data_permissions",
    )
    op.drop_index(
        "ix_data_permissions_role_conn_schema_table",
        table_name="data_permissions",
    )
    op.drop_table("data_permissions")

    # Reverse the embeddings ALTER. Drop the UNIQUE we added (only if
    # we own it — initdb-managed DBs keep their copy).
    op.execute(
        "ALTER TABLE pyrene_schema_embeddings "
        "DROP CONSTRAINT IF EXISTS pyrene_schema_embeddings_unique_target;"
    )
    # Step C reverse — drop NOT NULL.
    op.execute(
        "ALTER TABLE pyrene_schema_embeddings "
        "ALTER COLUMN connection_id DROP NOT NULL;"
    )
    # Step A reverse — drop the column. The backfill (Step B) is
    # lossless because every row was set to the same sentinel; dropping
    # the column erases that derived data without affecting the source
    # `(schema, "table")` identity.
    op.execute(
        "ALTER TABLE pyrene_schema_embeddings "
        "DROP COLUMN IF EXISTS connection_id;"
    )
