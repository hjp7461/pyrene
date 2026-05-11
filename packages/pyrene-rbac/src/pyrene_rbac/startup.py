"""Hook registration helper — wires the RBAC hook into a `Gateway`.

PLAN-010 Day 2 guardrail: gateway.py is OFF LIMITS for this PLAN.
Registration must happen at app startup via this module so the
gateway package stays plan-009-only.

Usage:

```python
resolver = PermissionResolver()
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

from pyrene_gateway import BeforeRunHook, Gateway
from pyrene_gateway.constants import PRIORITY_TOOL_RBAC
from pyrene_rbac.hooks import RoleLookup, SessionFactory, make_rbac_hook
from pyrene_rbac.permission_resolver import (
    DEFAULT_CONNECTION_ID,
    PermissionResolver,
)


def register_hooks(
    gateway: Gateway,
    *,
    resolver: PermissionResolver,
    session_factory: SessionFactory,
    role_lookup: RoleLookup,
    connection_id: UUID = DEFAULT_CONNECTION_ID,
) -> BeforeRunHook:
    """Build the RBAC hook and register it at PRIORITY_TOOL_RBAC.

    Importing this module does NOT register anything — callers must
    invoke `register_hooks(...)` from their lifespan. This keeps
    pytest collections free of side effects (BRIEF §6.1-3).
    """
    hook = make_rbac_hook(
        resolver,
        session_factory=session_factory,
        role_lookup=role_lookup,
        connection_id=connection_id,
    )
    gateway.before_run(hook, priority=PRIORITY_TOOL_RBAC)
    return hook


__all__ = ["register_hooks"]
