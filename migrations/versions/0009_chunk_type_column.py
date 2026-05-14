"""0009 chunk_type column: pyrene_schema_embeddings Hybrid chunk strategy.

Revision ID: 0009_chunk_type_column
Revises: 0008_budget_limits
Create Date: 2026-05-14

PRD-042 / PLAN-042 Wave 1 / ADR-020. Adds the columns required to host
both *table* and *column* chunks in the same `pyrene_schema_embeddings`
table — the substrate of the Hybrid chunk strategy.

### Schema delta

  - `pyrene_schema_embeddings`:
      - `+ chunk_type TEXT NOT NULL DEFAULT 'table'` with
        `CHECK (chunk_type IN ('table','column'))` — closed enum.
      - `+ column_name TEXT NOT NULL DEFAULT ''` — sentinel `''` for table
        chunks, actual column name for column chunks. Sentinel pattern
        avoids `NULL is distinct` semantics in the UNIQUE constraint.
  - UNIQUE constraint `pyrene_schema_embeddings_unique_target`:
      - was `(connection_id, schema, "table")`
      - becomes `(connection_id, schema, "table", chunk_type, column_name)`

### Compatibility

Existing rows are preserved as `chunk_type='table', column_name=''` via the
column DEFAULTs — no data migration needed. Column chunks are emitted by
re-running `pyrene-sql index-schema --reindex` (idempotent UPSERT).

### Downgrade

`column` chunks must be deleted before the old UNIQUE constraint is
restored (otherwise multiple rows for the same `(cid, schema, table)`
would conflict). Downgrade is intended for rollback during PR iteration;
operational rollback is `git revert <merge>` followed by this migration's
downgrade against a fresh DB.

### Initdb sync (operational note)

`deploy/postgres/initdb/03-schema-embeddings.sql` is updated in the same
PR to declare the same columns + UNIQUE on first boot. `initdb` runs
*before* alembic — fresh containers see the final schema directly; only
upgrade-from-existing flows execute this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_chunk_type_column"
down_revision: str | None = "0008_budget_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE pyrene_schema_embeddings
        ADD COLUMN IF NOT EXISTS chunk_type TEXT NOT NULL DEFAULT 'table'
            CHECK (chunk_type IN ('table', 'column'))
        """
    )
    op.execute(
        """
        ALTER TABLE pyrene_schema_embeddings
        ADD COLUMN IF NOT EXISTS column_name TEXT NOT NULL DEFAULT ''
        """
    )
    op.execute(
        "ALTER TABLE pyrene_schema_embeddings "
        "DROP CONSTRAINT IF EXISTS pyrene_schema_embeddings_unique_target"
    )
    op.execute(
        """
        ALTER TABLE pyrene_schema_embeddings
        ADD CONSTRAINT pyrene_schema_embeddings_unique_target
        UNIQUE (connection_id, schema, "table", chunk_type, column_name)
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM pyrene_schema_embeddings WHERE chunk_type = 'column'"
    )
    op.execute(
        "ALTER TABLE pyrene_schema_embeddings "
        "DROP CONSTRAINT IF EXISTS pyrene_schema_embeddings_unique_target"
    )
    op.execute(
        """
        ALTER TABLE pyrene_schema_embeddings
        ADD CONSTRAINT pyrene_schema_embeddings_unique_target
        UNIQUE (connection_id, schema, "table")
        """
    )
    op.execute(
        "ALTER TABLE pyrene_schema_embeddings DROP COLUMN IF EXISTS column_name"
    )
    op.execute(
        "ALTER TABLE pyrene_schema_embeddings DROP COLUMN IF EXISTS chunk_type"
    )
