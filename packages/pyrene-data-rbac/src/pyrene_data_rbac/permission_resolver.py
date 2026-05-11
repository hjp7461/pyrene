"""DataPermissionResolver — read-through TTLCache over `data_permissions`.

PLAN-011 Day 2 + ADR-008 cache pattern. Mirrors `pyrene-rbac`'s
`PermissionResolver` shape (same TTLCache size/TTL defaults, same
write-through invalidation pattern) so the host app can wire both
resolvers behind a single startup hook chain.

### Cache shape (ADR-008)

```
TTLCache(maxsize=1024, ttl=60)
```

Key tuple:
```
(role_ids_sorted: tuple[UUID, ...], connection_id: UUID,
 schema_norm: str, table_norm: str)
```

- `role_ids_sorted`  — sorted so `(a, b)` and `(b, a)` hit the same
  entry.
- `connection_id`    — the data-RBAC matrix is per-connection by
  design; PRD-011 §F2 forbids cross-connection bleed.
- `schema_norm` / `table_norm` — lowered + stripped, matching the
  schema-layer normalization on write and the hook-layer
  normalization on read (PM amend bypass cases 1-5).

### Decision algorithm — deny-precedence over default-deny, with
### wildcard / explicit tiering

For a `(role_ids, connection_id, schema, table)` query:

1. SELECT every row where `role_id IN (role_ids) AND connection_id = ?`
   (one DB hit per cache miss; rows for a single connection are
   bounded — PRD-011 §3.1 row count).
2. Partition rows by (schema-match, table-match) into four buckets:
     - **explicit**: row.schema == schema AND row.table == table
     - **schema_wildcard**: row.schema == schema AND row.table == '*'
     - **table_wildcard**: row.schema == '*' AND row.table == table
       (rare; PRD-011 keeps the matrix asymmetric — schema wildcards
       are the common case — but supporting both keeps the matcher
       symmetric and avoids surprise).
     - **full_wildcard**: row.schema == '*' AND row.table == '*'
3. Apply deny-precedence at the **most specific** tier first:
     - any explicit `deny` → **deny**.
     - any explicit `allow` → **allow** (unless another explicit deny
       exists — handled by point 1 above).
     - else any schema_wildcard or table_wildcard `deny` → **deny**.
     - else any schema_wildcard or table_wildcard `allow` → **allow**.
     - else any full_wildcard `deny` → **deny**.
     - else any full_wildcard `allow` → **allow**.
     - else **deny** (default-deny / PRD-011 §F1).

The "explicit deny beats wildcard allow" property is what PRD-011
§위험 #3 demands so an admin can punch a hole for one table in an
otherwise-wildcard role.

### Invalidation (write-through, ADR-008)

`invalidate_role(role_id)` is called from the CRUD endpoints **after
commit** (ADR-008 §3 — commit-before-invalidate keeps stale-but-correct
on rollback). The invalidation drops every cache entry whose key set
contains the role id; this is the same pattern as
`pyrene_rbac.PermissionResolver`.

### Fail-closed (PRD-011 §F1)

DB lookup raises → caller's hook propagates → Gateway.run() re-raises
→ FastAPI handler maps to 403. We do NOT catch in the resolver; the
data-RBAC hook translates the exception into a `PermissionDeniedError`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final
from uuid import UUID

from cachetools import TTLCache
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_data_rbac.repository import list_permissions_for_roles_on_connection

# Phase 1 default connection sentinel — must match
# `pyrene_sql.schema.models.DEFAULT_CONNECTION_ID` and the value
# stamped into `pyrene_schema_embeddings.connection_id` by initdb /
# the 0007 migration backfill. Single-connection deployments use this
# value end-to-end.
DEFAULT_CONNECTION_ID: Final[UUID] = UUID("00000000-0000-0000-0000-000000000001")

# Wildcard sentinel — must match `schemas.WILDCARD`.
_WILDCARD: Final[str] = "*"

# Cache key alias keeps mypy --strict happy and gives the test suite
# a name to assert against.
_CacheKey = tuple[tuple[UUID, ...], UUID, str, str]


def _normalize(value: str) -> str:
    """Strip whitespace, strip surrounding double quotes, lowercase.

    Mirrors `schemas._normalize_identifier`. The hook layer normalizes
    inbound identifiers with this transform, so `PUBLIC.payment`,
    `"public"."payment"`, ` public . payment ` all collapse onto the
    canonical `public.payment` storage form (PM amend, schema-qualified
    bypass cases 1-5).
    """
    return value.strip().strip('"').lower()


def _make_key(
    role_ids: tuple[UUID, ...],
    connection_id: UUID,
    schema: str,
    table: str,
) -> _CacheKey:
    """Canonical cache key. Sorts role_ids so caller order does not
    fragment the cache."""
    return (
        tuple(sorted(role_ids)),
        connection_id,
        _normalize(schema),
        _normalize(table),
    )


class DataPermissionResolver:
    """RBAC decision oracle for the data-level matrix.

    Construct one per process (typically at app startup) and inject
    into both the data-RBAC hook factory and the CRUD route handlers
    so write paths can call `invalidate_role(...)` after commit.

    Constructor accepts `maxsize` / `ttl` so the test suite can
    fabricate short-TTL instances. Defaults track ADR-008 (1024 / 60s).
    """

    def __init__(self, *, maxsize: int = 1024, ttl: float = 60.0) -> None:
        # TTLCache is not async-safe in the strict sense, but the
        # gateway is single-process Phase 2 (ADR-008) and the only
        # writers are the CRUD endpoints which run under asyncio's
        # cooperative scheduler.
        self._cache: TTLCache[_CacheKey, bool] = TTLCache(
            maxsize=maxsize, ttl=ttl
        )

    # ----- Read path ---------------------------------------------------------

    async def can_access(
        self,
        session: AsyncSession,
        *,
        role_ids: tuple[UUID, ...],
        connection_id: UUID,
        schema: str,
        table: str,
    ) -> bool:
        """Return True iff the union of `role_ids` may read
        `(connection_id, schema, table)`.

        Empty `role_ids` → deny (default-deny / no-role-no-access).
        DB error → propagates (caller maps to fail-closed).
        """
        if not role_ids:
            return False

        norm_schema = _normalize(schema)
        norm_table = _normalize(table)
        key = _make_key(role_ids, connection_id, norm_schema, norm_table)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        rows = await list_permissions_for_roles_on_connection(
            session, role_ids, connection_id
        )

        decision = self._evaluate(rows, norm_schema, norm_table)
        self._cache[key] = decision
        return decision

    # ----- Decision algorithm (pure function, easy to unit-test) -------------

    @staticmethod
    def _evaluate(
        rows: Iterable[object],  # DataPermission rows; typed loose for tests
        schema: str,
        table: str,
    ) -> bool:
        """Apply explicit > wildcard, deny-wins-within-tier.

        See module docstring §"Decision algorithm" for the policy spec.
        Each row is duck-typed: anything with `.schema`, `.table_name`,
        and `.action` works. Tests pass `_FakeRow` objects with the
        same shape so this function doesn't import the SQLAlchemy
        model at type-check time.
        """
        # Bucket rows into tiers.
        explicit_allow = False
        explicit_deny = False
        partial_allow = False  # schema OR table wildcard, not both
        partial_deny = False
        full_allow = False
        full_deny = False

        for row in rows:
            r_schema = _normalize(row.schema)  # type: ignore[attr-defined]
            r_table = _normalize(row.table_name)  # type: ignore[attr-defined]
            r_action = row.action  # type: ignore[attr-defined]

            schema_match = r_schema == schema
            table_match = r_table == table
            schema_wild = r_schema == _WILDCARD
            table_wild = r_table == _WILDCARD

            if not (schema_match or schema_wild):
                continue
            if not (table_match or table_wild):
                continue

            if schema_match and table_match:
                if r_action == "deny":
                    explicit_deny = True
                else:
                    explicit_allow = True
            elif schema_wild and table_wild:
                if r_action == "deny":
                    full_deny = True
                else:
                    full_allow = True
            else:
                # schema_wild XOR table_wild — partial wildcard tier.
                if r_action == "deny":
                    partial_deny = True
                else:
                    partial_allow = True

        # Tier 1: explicit (highest specificity).
        if explicit_deny:
            return False
        if explicit_allow:
            return True
        # Tier 2: partial wildcard (one axis exact, one '*').
        if partial_deny:
            return False
        if partial_allow:
            return True
        # Tier 3: full wildcard.
        if full_deny:
            return False
        # Default-deny (PRD-011 §F1) — `full_allow` is the last
        # remaining tier; absent that, no row grants access.
        return full_allow

    # ----- Write-through invalidation (ADR-008 §3) ---------------------------

    def invalidate_role(self, role_id: UUID) -> None:
        """Drop every cached decision whose key set includes `role_id`.

        CRUD endpoints call this AFTER `session.commit()` so a rollback
        leaves the cache in the (stale-but-correct) pre-write state.
        Iteration over `_cache.keys()` is safe under single-threaded
        asyncio because no concurrent task yields between snapshot and
        delete.
        """
        to_drop = [k for k in list(self._cache.keys()) if role_id in k[0]]
        for k in to_drop:
            self._cache.pop(k, None)

    def invalidate_connection(self, connection_id: UUID) -> None:
        """Drop every cached decision for a connection.

        Used by the (future) connection-edit endpoints — a connection
        being decommissioned must not leave stale allow rows alive in
        the resolver.
        """
        to_drop = [k for k in list(self._cache.keys()) if k[1] == connection_id]
        for k in to_drop:
            self._cache.pop(k, None)

    def invalidate_all(self) -> None:
        """Clear the entire cache. Use for bulk YAML import."""
        self._cache.clear()

    # ----- Test seam ---------------------------------------------------------

    def _cache_size(self) -> int:
        """Test helper — current cache occupancy."""
        return len(self._cache)

    def _cache_keys(self) -> tuple[_CacheKey, ...]:
        """Test helper — snapshot the live cache key set."""
        return tuple(self._cache.keys())


__all__ = [
    "DEFAULT_CONNECTION_ID",
    "DataPermissionResolver",
]
