# pyrene-rbac

Tool-level RBAC (Role × Tool matrix) — PRD-010 / PLAN-010.

**ADR-007 owner: PLAN-010** (row/column masking deferred to Phase 1.5;
the protection unit for this PLAN is **tool invocations**).

**ADR-008 applied**: `cachetools.TTLCache(maxsize=1024, ttl=60)` +
write-through invalidation on every CRUD commit.

**ADR-013 (b) applied**: `permissions.role_id` → `roles.id` ON DELETE
**RESTRICT** — accidental role drops must not silently strip privileges
from every user holding that role.

## Public surface

| Symbol | Purpose |
|---|---|
| `Permission` / `metadata` | SQLAlchemy model + shared MetaData |
| `PermissionResolver` | TTLCache-backed decision oracle |
| `make_rbac_hook(...)` | Hook factory; returns a `BeforeRunHook` |
| `register_hooks(gateway, ...)` | Registers at `PRIORITY_TOOL_RBAC` (=20) |
| `permissions_router` + `set_resolver(...)` | Admin-only `/rbac/*` CRUD |

The package leaves `pyrene-gateway` source untouched — registration
happens at host-app startup via `register_hooks(...)`.

## Migration

`migrations/versions/0004_rbac_matrix.py` (chain: 0003 → 0004).

## Phase boundary

- Phase 2 (this PLAN): single-process; cache key carries the
  `DEFAULT_CONNECTION_ID` sentinel; `connection_id` slot is reserved
  for PLAN-011 (data RBAC) to extend the same key without re-shaping.
- Phase 1.5: row/column masking — see ADR-007.
- Phase 3: multi-worker cache invalidation — see ADR-008 follow-up.
