"""PermissionResolver — read-through TTLCache over the `permissions` table.

PLAN-010 Day 2 + ADR-008 cache pattern. The resolver is the single
component every code path consults to ask "may this user invoke this
tool?". It is intentionally NOT a global singleton (BRIEF §6.1-3):
host apps construct one instance and pass it to the RBAC hook factory
+ the FastAPI router state.

### Cache shape (ADR-008)

```
TTLCache(maxsize=1024, ttl=60)
```

Key tuple:
```
(role_ids_sorted: tuple[UUID, ...], tool_name_norm: str, connection_id: UUID)
```

- `role_ids_sorted` — sorted so two callers passing
  `(role_a, role_b)` and `(role_b, role_a)` hit the same entry.
- `tool_name_norm`  — stripped + lowercased (F-02 exact match is
  enforced by ALSO normalizing both sides at DB write time via the
  schema layer; the resolver normalizes its arg to match).
- `connection_id`   — Phase 2 ships a sentinel UUID
  (`DEFAULT_CONNECTION_ID`) because data-RBAC scoping (PLAN-011) is
  out of scope here. Adding it to the key today means PLAN-011 can
  reuse the same key shape later without rewriting cached hits as
  cache misses (ADR-008 multi-tenant key requirement).

### Decision algorithm — deny-precedence over default-deny

For a `(role_ids, tool_name)` query:

1. SELECT every row where `role_id IN (role_ids)` and `tool_name = ?`.
2. If any row has `action="deny"` -> **deny** (explicit revocation
   wins, PRD-010 §4).
3. Else if any row has `action="allow"` -> **allow**.
4. Else -> **deny** (default-deny / PRD-010 §2.2 F1).

### Invalidation (write-through, ADR-008)

`invalidate_role(role_id)` is called from the CRUD endpoints **after
commit** (ADR-008 §3 — commit-before-invalidate keeps stale-but-correct
on rollback). A role-scoped invalidation iterates the live cache and
drops every entry whose key includes that role id. For bulk imports
the route can call `invalidate_all()` (== `cache.clear()`).

### Fail-closed (PRD-010 §2.2 F2)

DB lookup raises -> caller's hook propagates the exception ->
Gateway.run() re-raises -> FastAPI handler maps to 403. We do NOT
catch in the resolver; the RBAC hook's job is to translate the
exception into a `PermissionDeniedError` (`pyrene_core.errors`).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from cachetools import TTLCache
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_rbac.repository import list_permissions_for_roles

# Phase 2 sentinel — `connection_id` is in the cache key so PLAN-011
# (data RBAC) can extend the same resolver without breaking the key
# shape. Until then, every Phase 2 lookup goes through this sentinel.
DEFAULT_CONNECTION_ID: Final[UUID] = UUID("00000000-0000-0000-0000-000000000000")


# Internal cache key. Module-level type alias keeps mypy --strict happy
# and gives the test suite a name to assert against.
_CacheKey = tuple[tuple[UUID, ...], str, UUID]


def _normalize_tool_name(name: str) -> str:
    """Strip + lowercase. Mirrors the schema-layer normalization so
    bypass attempts via `Run_Select` / ` run_select ` do not slip past
    the resolver (PLAN-010 Day 3 wave). The DB column is case-sensitive
    by default in Postgres; normalization happens at both write
    (schema validator) and read (this function)."""
    return name.strip().lower()


def _make_key(
    role_ids: tuple[UUID, ...], tool_name: str, connection_id: UUID
) -> _CacheKey:
    """Build the canonical cache key. Sort role_ids so caller order
    does not split the cache."""
    return (tuple(sorted(role_ids)), _normalize_tool_name(tool_name), connection_id)


class PermissionResolver:
    """RBAC decision oracle for the tool-level matrix.

    Construct one per process (typically at app startup) and inject
    into both the RBAC hook factory and the CRUD route handlers so
    write paths can call `invalidate_role(...)` after commit.

    Constructor accepts `maxsize` / `ttl` so the test suite can
    fabricate short-TTL instances. Defaults track ADR-008 (1024 / 60s).
    """

    def __init__(
        self, *, maxsize: int = 1024, ttl: float = 60.0
    ) -> None:
        # TTLCache is not async-safe in the strict sense, but the
        # gateway is single-process Phase 2 (ADR-008) and the only
        # writers are the CRUD endpoints which run under the asyncio
        # event loop's cooperative scheduler. No GIL races; no lock
        # needed at this layer.
        self._cache: TTLCache[_CacheKey, bool] = TTLCache(
            maxsize=maxsize, ttl=ttl
        )

    # ----- Read path ---------------------------------------------------------

    async def can_invoke(
        self,
        session: AsyncSession,
        *,
        role_ids: tuple[UUID, ...],
        tool_name: str,
        connection_id: UUID = DEFAULT_CONNECTION_ID,
    ) -> bool:
        """Return True iff the union of `role_ids` may invoke `tool_name`.

        Empty `role_ids` -> deny (default-deny / no-role-no-access).
        DB error -> propagates (caller maps to fail-closed).
        """
        if not role_ids:
            return False

        key = _make_key(role_ids, tool_name, connection_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        rows = await list_permissions_for_roles(
            session, role_ids, _normalize_tool_name(tool_name)
        )
        # Deny precedence: any deny row -> deny. Default-deny if no
        # allow row present (PRD-010 §2.2 F1).
        has_deny = any(r.action == "deny" for r in rows)
        has_allow = any(r.action == "allow" for r in rows)
        decision = (not has_deny) and has_allow

        self._cache[key] = decision
        return decision

    # ----- Write-through invalidation (ADR-008 §3) ---------------------------

    def invalidate_role(self, role_id: UUID) -> None:
        """Drop every cached decision whose key set includes `role_id`.

        CRUD endpoints call this AFTER `session.commit()` so a rollback
        leaves the cache in the (stale-but-correct) pre-write state.
        Iteration over `_cache.keys()` is safe under single-threaded
        asyncio because no concurrent task yields between snapshot and
        delete.
        """
        # Snapshot the keys; cachetools' KeysView is a live mapping
        # proxy, so we materialize to a list to avoid mutating during
        # iteration.
        to_drop = [k for k in list(self._cache.keys()) if role_id in k[0]]
        for k in to_drop:
            self._cache.pop(k, None)

    def invalidate_all(self) -> None:
        """Clear the entire cache. Use for bulk YAML import in PLAN-010
        Day 3 (Phase 1 ships per-row CRUD; bulk import lands later)."""
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
    "PermissionResolver",
]
