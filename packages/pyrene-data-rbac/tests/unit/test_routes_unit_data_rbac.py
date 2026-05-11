"""Unit tests for the data-RBAC route module's helpers.

The full HTTP CRUD flow runs in `tests/integration/test_routes_db_data_rbac.py`
against a real Postgres. These tests cover what runs without a DB:

  - `set_resolver` / `reset_resolver` round-trip.
  - `_get_resolver()` raises when nothing is wired (defends against
    silent misconfiguration at app startup).
  - The router exposes the four PRD-011 §4 endpoints.
"""

from __future__ import annotations

import pytest

from pyrene_data_rbac import DataPermissionResolver
from pyrene_data_rbac.routes import data_permissions as routes_module


def test_get_resolver_raises_when_unwired() -> None:
    """Fresh module-level slot → `_get_resolver` raises loudly."""
    routes_module.reset_resolver()
    with pytest.raises(RuntimeError, match="resolver is not configured"):
        routes_module._get_resolver()


def test_set_and_reset_resolver() -> None:
    resolver = DataPermissionResolver()
    routes_module.set_resolver(resolver)
    assert routes_module._get_resolver() is resolver
    routes_module.reset_resolver()
    with pytest.raises(RuntimeError):
        routes_module._get_resolver()


def test_data_permissions_router_route_table() -> None:
    """Smoke: every PRD-011 §4 endpoint is bound."""
    from pyrene_data_rbac import data_permissions_router

    paths: set[tuple[str, tuple[str, ...]]] = set()
    for route in data_permissions_router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods:
            continue
        paths.add((path, tuple(sorted(methods))))
    assert ("/rbac/data-permissions", ("GET",)) in paths
    assert ("/rbac/data-permissions", ("POST",)) in paths
    assert (
        "/rbac/data-permissions/{permission_id}",
        ("PUT",),
    ) in paths
    assert (
        "/rbac/data-permissions/{permission_id}",
        ("DELETE",),
    ) in paths
