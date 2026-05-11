"""Routes package for pyrene-data-rbac.

Public re-exports keep the host app wiring symmetrical with
`pyrene_rbac.routes` (one router + `set_resolver` / `reset_resolver`).
"""

from pyrene_data_rbac.routes.data_permissions import (
    data_permissions_router,
    reset_resolver,
    set_resolver,
)

__all__ = [
    "data_permissions_router",
    "reset_resolver",
    "set_resolver",
]
