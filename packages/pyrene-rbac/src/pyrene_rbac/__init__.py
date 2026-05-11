"""Pyrene tool-level RBAC (PRD-010 / PLAN-010).

Wave 7 isolated package. The hook factory + resolver + route module
deliberately leave `pyrene_gateway.gateway.Gateway` untouched — host
apps wire the hook through `register_hooks(gateway, ...)` at startup.

`ADR-007 owner: PLAN-010` (header line — row/column masking deferral
is this PLAN's owner pre-condition).

Public surface:

- `Permission` / `metadata`            — SQLAlchemy model + shared metadata
- `PermissionResolver`                 — TTLCache-backed decision oracle
- `make_rbac_hook` / `register_hooks`  — hook factory + gateway wiring
- `permissions_router` / `set_resolver`/ `reset_resolver`
                                       — admin CRUD + matrix view
- schemas (`PermissionCreateRequest`, `MatrixResponse`, ...)
"""

from pyrene_rbac.hooks import (
    RoleLookup,
    SessionFactory,
    make_rbac_hook,
)
from pyrene_rbac.models import Base, Permission, metadata
from pyrene_rbac.permission_resolver import (
    DEFAULT_CONNECTION_ID,
    PermissionResolver,
)
from pyrene_rbac.routes import permissions_router, reset_resolver, set_resolver
from pyrene_rbac.schemas import (
    MatrixResponse,
    MatrixRoleEntry,
    PermissionAction,
    PermissionCreateRequest,
    PermissionResponse,
    PermissionUpdateRequest,
)
from pyrene_rbac.startup import register_hooks

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_CONNECTION_ID",
    "Base",
    "MatrixResponse",
    "MatrixRoleEntry",
    "Permission",
    "PermissionAction",
    "PermissionCreateRequest",
    "PermissionResolver",
    "PermissionResponse",
    "PermissionUpdateRequest",
    "RoleLookup",
    "SessionFactory",
    "make_rbac_hook",
    "metadata",
    "permissions_router",
    "register_hooks",
    "reset_resolver",
    "set_resolver",
]
