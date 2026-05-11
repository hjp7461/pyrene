# pyrene-data-rbac

Data-level RBAC (Role × Connection × Schema × Table matrix) — PRD-011 / PLAN-011.

This package controls **which data** a role may read after `pyrene-rbac`
(tool RBAC) has already approved the tool call itself. The protection
unit is Connection → Database → Schema → Table (ADR-007 defers row /
column masking to Phase 1.5).

**ADR-008 applied**: `cachetools.TTLCache(maxsize=1024, ttl=60)` +
write-through invalidation on every CRUD commit. Cache key includes
`connection_id` so multi-tenant deployments do not bleed.

**ADR-013 (b) applied**: `data_permissions.role_id` →
`roles.id` ON DELETE **RESTRICT**.

**ADR-013 (c) applied**: `pyrene_schema_embeddings.connection_id`
column shipped via the canonical ADD COLUMN NULL → backfill → SET NOT
NULL three-step migration (HNSW index is graph-based so no REINDEX is
required).

## Hook placement

Registers a `BeforeRunHook` at **`PRIORITY_DATA_RBAC = 30`** — runs
after tool-RBAC (20) and before tool execution. Hook reads the tool's
`table` (or `base_table`, `left`, `right`, `join.table` for the JOIN
and aggregate tools) from `RunContext.metadata["tool_input"]` and
denies fast if any referenced table is not granted to the caller's
role set on the active `connection_id`.

## Public surface

| Symbol | Purpose |
|---|---|
| `DataPermission` / `metadata` | SQLAlchemy model + shared MetaData |
| `DataPermissionResolver` | TTLCache-backed decision oracle |
| `make_data_rbac_hook(...)` | Hook factory returning a `BeforeRunHook` |
| `register_hooks(gateway, ...)` | Registers at `PRIORITY_DATA_RBAC` (=30) |
| `data_permissions_router` + `set_resolver(...)` | Admin-only `/rbac/data-permissions/*` CRUD |

## Migration

`migrations/versions/0007_data_permissions.py` — chain
`0006_audit_log → 0007_data_permissions`.

- Creates `data_permissions` table (fresh; no ALTER required).
- Adds `pyrene_schema_embeddings.connection_id` via the ADD COLUMN
  NULL → backfill → SET NOT NULL three-step pattern when the column
  is not yet present (initdb-bootstrapped DBs already carry the
  column, so the migration is idempotent against both paths).
- Adds `UNIQUE(connection_id, schema, "table")` on
  `pyrene_schema_embeddings` to match the PLAN-002 retriever.

## F-03 dual defense

The hook is the **code guard** half of F-03 (BRIEF §6 dual defense).
The other half is the Postgres `pyrene_readonly` role created by
PLAN-001 / `deploy/postgres/initdb/02-readonly-role.sql`: even if a
caller bypasses the hook (somehow), the DB-level role lacks privilege
on tables outside the whitelist. Both paths are covered by
`tests/integration/test_dual_defense_data_rbac.py`.
