"""RBAC HTTP routes.

`permissions_router` exposes the admin-only CRUD + matrix view at
`/rbac/*`. The host app includes it on the main FastAPI instance and
injects a `PermissionResolver` via `set_resolver(...)` so write-side
endpoints can invalidate the cache after commit.
"""

from pyrene_rbac.routes.permissions import (
    permissions_router,
    reset_resolver,
    set_resolver,
)

__all__ = ["permissions_router", "reset_resolver", "set_resolver"]
