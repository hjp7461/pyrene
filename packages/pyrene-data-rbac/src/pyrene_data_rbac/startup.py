"""Hook registration helper — wires the data-RBAC hook into a `Gateway`.

PLAN-011 Day 2. gateway.py stays untouched; registration happens at
app startup via this module so `pyrene-gateway` remains plan-009-only.

Usage:

```python
resolver = DataPermissionResolver()
register_hooks(
    gateway,
    resolver=resolver,
    session_factory=my_session_factory,
    role_lookup=resolve_roles_by_name,
)
```

Returns the constructed hook callable so the caller (typically a
FastAPI lifespan) can keep a handle for diagnostics or test-time
unregister via a fresh gateway.
"""

from __future__ import annotations

from uuid import UUID

from pyrene_data_rbac.hooks import (
    RoleLookup,
    SessionFactory,
    make_data_rbac_hook,
)
from pyrene_data_rbac.permission_resolver import (
    DEFAULT_CONNECTION_ID,
    DataPermissionResolver,
)
from pyrene_gateway import BeforeRunHook, Gateway
from pyrene_gateway.constants import PRIORITY_DATA_RBAC


def register_hooks(
    gateway: Gateway,
    *,
    resolver: DataPermissionResolver,
    session_factory: SessionFactory,
    role_lookup: RoleLookup,
    default_connection_id: UUID = DEFAULT_CONNECTION_ID,
) -> BeforeRunHook:
    """Build the data-RBAC hook and register it at PRIORITY_DATA_RBAC.

    Importing this module does NOT register anything — callers must
    invoke `register_hooks(...)` from their lifespan. This keeps
    pytest collections free of side effects (BRIEF §6.1-3).
    """
    hook = make_data_rbac_hook(
        resolver,
        session_factory=session_factory,
        role_lookup=role_lookup,
        default_connection_id=default_connection_id,
    )
    gateway.before_run(hook, priority=PRIORITY_DATA_RBAC)
    return hook


__all__ = ["register_hooks"]
