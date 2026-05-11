"""0006 audit log: audit_events (WORM) + dual defense + per-team hash chain.

Revision ID: 0006_audit_log
Revises: 0003_mcp_gateway
Create Date: 2026-05-11

PLAN-015 Day 1. Wave 7 parallel — PLAN-010 takes 0004, PLAN-013 takes 0005,
this migration claims 0006. We declare `down_revision = 0003_mcp_gateway`
because PLAN-010 / PLAN-013 schemas do not yet exist on disk; Alembic
resolves the chain order at upgrade time (the in-flight branches will
rebase `down_revision` onto 0005 once they merge, since audit has no
schema dependency on the cost / RBAC tables — only on auth's
`users` / `teams`, which are at 0001).

### WORM dual defense (PRD-015 §3.1 + PLAN-015 §Day 1)

(a) `audit_worm_guard()` BEFORE UPDATE / DELETE / TRUNCATE trigger —
    RAISEs EXCEPTION unless `current_setting('audit.bypass', true) = 'on'`
    (super-role bypass for in-place migrations).
(b) `REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM PUBLIC, pyrene_app;`
    — the application role lacks the privilege even if a hostile actor
    bypasses (a) somehow.
(c) Trigger fires at statement level (not row level) so `TRUNCATE` is
    also intercepted.

### Hash chain (per-team)

The BEFORE INSERT trigger `audit_set_row_hash` (defined here):
  - looks up the chain tip for the inserted row's `team_id`
    `(team_id, created_at desc, id desc)` index — added in this
    migration — gives the trigger a sub-millisecond lookup;
  - sets `NEW.prev_hash` to the tip's `row_hash` (NULL on first row);
  - computes `NEW.row_hash = sha256(coalesce(prev_hash, \\x00) ||
    canonical(NEW.* without row_hash))` using `pgcrypto.digest`.

Application code MUST NOT supply `prev_hash` or `row_hash` — the trigger
overwrites whatever is passed. The DBAuditSink omits them from the
INSERT.

The serializing read is bounded by `team_id`: cross-team INSERTs do not
contend (different chain tips), and within a team the chain tip lookup
hits the dedicated index.

### Concurrency note (PLAN-015 §위험 신호 #6)

Two concurrent INSERTs into the same team can both read the same
`prev_hash` and produce two siblings linking to the same parent. Phase
2 ships the simple form (no advisory lock) because:
  - Phase 2 traffic is sub-second per team, contention is theoretical.
  - The audit hook is fail-closed; if a sibling collides on `row_hash`
    by coincidence, an out-of-band verifier surfaces it.
Phase 3 promotion either (a) takes a `pg_advisory_xact_lock(hashtext('team:'||team_id))`
inside the trigger, or (b) introduces a `chain_seq` BIGSERIAL column for
strict total order. Both are migration-only changes; the wire format
does not move.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_audit_log"
# Wave 7 chain reality: at the time of authoring, PLAN-010 (0004) and
# PLAN-013 (0005) had both landed on the integration branch with their
# own `down_revision = 0003_mcp_gateway` (parallel feature branches did
# not yet rebase). The PM-coordinated landing order is
# PLAN-010 → PLAN-013 → PLAN-015; this migration claims 0005 as its
# parent so the chain is linear. If 0005 is absent in an isolated
# feature branch, the author rebases this revision onto the chain head
# at PR review time.
down_revision: str | None = "0005_cost_metering"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_WORM_GUARD_FN = """
CREATE OR REPLACE FUNCTION audit_worm_guard() RETURNS trigger AS $$
BEGIN
  IF current_setting('audit.bypass', true) = 'on' THEN
    -- Super-role bypass. Only set inside admin migrations via
    -- `SET LOCAL audit.bypass = 'on'`; the GUC is transaction-scoped
    -- so it cannot leak across statements.
    IF TG_OP = 'DELETE' THEN
      RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
      RETURN NEW;
    ELSE
      RETURN NULL;
    END IF;
  END IF;
  RAISE EXCEPTION
    'audit_events is WORM (TG_OP=%); set audit.bypass=on in a super-role txn to override',
    TG_OP
    USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""


_HASH_FN = """
CREATE OR REPLACE FUNCTION audit_set_row_hash() RETURNS trigger AS $$
DECLARE
  tip bytea;
  payload text;
BEGIN
  -- Chain tip lookup (per-team). Index ix_audit_events_team_chain_tip
  -- makes this O(log n) within the team's history.
  IF NEW.team_id IS NULL THEN
    tip := NULL;
  ELSE
    SELECT row_hash INTO tip
    FROM audit_events
    WHERE team_id = NEW.team_id
    ORDER BY created_at DESC, id DESC
    LIMIT 1;
  END IF;

  NEW.prev_hash := tip;

  -- Canonicalize the row minus row_hash itself (`prev_hash` participates).
  -- `jsonb_build_object` orders keys deterministically; serialization
  -- is `::text` so external re-verification can mirror the exact bytes.
  payload := jsonb_build_object(
    'id',         NEW.id,
    'event_type', NEW.event_type,
    'user_id',    NEW.user_id,
    'team_id',    NEW.team_id,
    'agent_id',   NEW.agent_id,
    'request_id', NEW.request_id,
    'tool_name',  NEW.tool_name,
    'outcome',    NEW.outcome,
    'metadata',   NEW.metadata,
    'created_at', NEW.created_at,
    'prev_hash',  CASE
                    WHEN NEW.prev_hash IS NULL THEN NULL
                    ELSE encode(NEW.prev_hash, 'hex')
                  END
  )::text;

  NEW.row_hash := digest(
    coalesce(NEW.prev_hash, '\\x00'::bytea) || convert_to(payload, 'UTF8'),
    'sha256'
  );

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # pgcrypto provides `digest(bytea, text)` used by the hash trigger.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name="fk_audit_events_user_id"
            ),
            nullable=True,
        ),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "teams.id", ondelete="RESTRICT", name="fk_audit_events_team_id"
            ),
            nullable=True,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("prev_hash", sa.LargeBinary, nullable=True),
        # row_hash is NOT NULL — but the application never supplies it; the
        # BEFORE INSERT trigger populates it before the row hits storage.
        sa.Column(
            "row_hash",
            sa.LargeBinary,
            nullable=False,
            server_default=sa.text("'\\x00'::bytea"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Indexes (PLAN-015 §Day 1 — six total: 5 btree + 1 BRIN; GIN deferred
    # to L-02 follow-up because Phase 2 doesn't query metadata containment).
    op.create_index(
        "ix_audit_events_user_created", "audit_events", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_audit_events_team_chain_tip",
        "audit_events",
        ["team_id", "created_at", "id"],
    )
    op.create_index(
        "ix_audit_events_event_type_created",
        "audit_events",
        ["event_type", "created_at"],
    )
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index(
        "ix_audit_events_agent_created", "audit_events", ["agent_id", "created_at"]
    )
    op.execute(
        "CREATE INDEX ix_audit_events_created_brin ON audit_events "
        "USING BRIN (created_at);"
    )

    # WORM guard + hash chain trigger functions + triggers.
    op.execute(_WORM_GUARD_FN)
    op.execute(_HASH_FN)
    op.execute(
        """
        CREATE TRIGGER audit_worm_trigger
        BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION audit_worm_guard();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_hash_chain_trigger
        BEFORE INSERT ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_set_row_hash();
        """
    )

    # Role-layer defense: revoke mutation from PUBLIC + the application
    # role. No-op when running as superuser in a fresh testcontainer
    # (pyrene_app doesn't exist), but still revokes from PUBLIC so the
    # GRANT layer is enforced even without the role split.
    op.execute(
        """
        DO $$
        BEGIN
          EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM PUBLIC';
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pyrene_app') THEN
            EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM pyrene_app';
            EXECUTE 'GRANT INSERT, SELECT ON audit_events TO pyrene_app';
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    # Downgrade must lift the WORM guard before dropping the table —
    # otherwise the BEFORE-anything trigger would block our own DROP if
    # PostgreSQL ever started routing DDL through the same trigger
    # surface. (Currently DDL bypasses the trigger; the lift is
    # belt-and-suspenders.)
    op.execute("SET LOCAL audit.bypass = 'on';")
    op.execute("DROP TRIGGER IF EXISTS audit_worm_trigger ON audit_events;")
    op.execute("DROP TRIGGER IF EXISTS audit_hash_chain_trigger ON audit_events;")
    op.execute("DROP FUNCTION IF EXISTS audit_worm_guard();")
    op.execute("DROP FUNCTION IF EXISTS audit_set_row_hash();")
    op.execute("DROP INDEX IF EXISTS ix_audit_events_created_brin;")
    op.drop_index("ix_audit_events_agent_created", table_name="audit_events")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index(
        "ix_audit_events_event_type_created", table_name="audit_events"
    )
    op.drop_index("ix_audit_events_team_chain_tip", table_name="audit_events")
    op.drop_index("ix_audit_events_user_created", table_name="audit_events")
    op.drop_table("audit_events")
