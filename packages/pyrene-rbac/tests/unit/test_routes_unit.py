"""Unit tests for the RBAC route module's helpers.

The full HTTP CRUD flow runs in `tests/integration/test_routes_db.py`
against a real Postgres. These tests cover what runs without a DB:

  - `set_resolver` / `reset_resolver` round-trip.
  - `_get_resolver()` raises when nothing is wired (defends against
    silent misconfiguration at app startup).
"""

from __future__ import annotations

import pytest

from pyrene_rbac import PermissionResolver
from pyrene_rbac.routes import permissions as routes_module


def test_get_resolver_raises_when_unwired() -> None:
    """Fresh module-level slot → calling `_get_resolver` is loud."""
    routes_module.reset_resolver()
    with pytest.raises(RuntimeError, match="resolver is not configured"):
        routes_module._get_resolver()


def test_set_and_reset_resolver() -> None:
    resolver = PermissionResolver()
    routes_module.set_resolver(resolver)
    assert routes_module._get_resolver() is resolver
    routes_module.reset_resolver()
    with pytest.raises(RuntimeError):
        routes_module._get_resolver()


def test_permissions_router_route_table() -> None:
    """Smoke: every PRD-010 §4 endpoint is bound."""
    from pyrene_rbac import permissions_router

    # `methods` is a frozenset on starlette Route — order-independent.
    paths: set[tuple[str, tuple[str, ...]]] = set()
    for route in permissions_router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods:
            continue
        paths.add((path, tuple(sorted(methods))))
    assert ("/rbac/permissions", ("GET",)) in paths
    assert ("/rbac/permissions", ("POST",)) in paths
    assert ("/rbac/permissions/{permission_id}", ("PUT",)) in paths
    assert ("/rbac/permissions/{permission_id}", ("DELETE",)) in paths
    assert ("/rbac/matrix", ("GET",)) in paths
