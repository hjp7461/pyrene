"""Pyrene data-level RBAC (PRD-011 / PLAN-011).

Wave 8 isolated package. The hook factory + resolver + route module
deliberately leave `pyrene_gateway.gateway.Gateway` untouched — host
apps wire the hook through `register_hooks(gateway, ...)` at startup.

Public surface:

- `DataPermission` / `metadata`        — SQLAlchemy model + shared metadata
- `DataPermissionResolver`             — TTLCache-backed decision oracle
- `make_data_rbac_hook` / `register_hooks`
                                       — hook factory + gateway wiring
- `data_permissions_router` / `set_resolver` / `reset_resolver`
                                       — admin CRUD
- `parse_qualified`                    — schema-qualified canonicalizer
                                         (PM amend bypass cases 1-5)
- schemas (`DataPermissionCreateRequest`, ...)
"""

from pyrene_data_rbac.hooks import (
    RoleLookup,
    SessionFactory,
    make_data_rbac_hook,
    parse_qualified,
)
from pyrene_data_rbac.models import Base, DataPermission, metadata
from pyrene_data_rbac.permission_resolver import (
    DEFAULT_CONNECTION_ID,
    DataPermissionResolver,
)
from pyrene_data_rbac.routes import (
    data_permissions_router,
    reset_resolver,
    set_resolver,
)
from pyrene_data_rbac.schemas import (
    WILDCARD,
    DataPermissionAction,
    DataPermissionCreateRequest,
    DataPermissionResponse,
    DataPermissionUpdateRequest,
)
from pyrene_data_rbac.startup import register_hooks

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_CONNECTION_ID",
    "WILDCARD",
    "Base",
    "DataPermission",
    "DataPermissionAction",
    "DataPermissionCreateRequest",
    "DataPermissionResolver",
    "DataPermissionResponse",
    "DataPermissionUpdateRequest",
    "RoleLookup",
    "SessionFactory",
    "data_permissions_router",
    "make_data_rbac_hook",
    "metadata",
    "parse_qualified",
    "register_hooks",
    "reset_resolver",
    "set_resolver",
]
