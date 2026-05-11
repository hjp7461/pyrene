"""PRIORITY_* constants are stable + ordered correctly.

PLAN-009 Day 3. PLAN-010/011/013/014/015 depend on these specific
values; any change here is a cross-PLAN breaking change. Lock the
order with this test so a stray reorder is caught in CI.
"""

from __future__ import annotations

from pyrene_gateway import (
    PRIORITY_AUDIT,
    PRIORITY_BUDGET_POST,
    PRIORITY_BUDGET_PRE,
    PRIORITY_DATA_RBAC,
    PRIORITY_TOOL_RBAC,
)


def test_priority_values_are_fixed() -> None:
    """Hardcoded values — PLAN-010/011/013/014/015 import these by name
    but the integer values are the contract that survives a rename."""
    assert PRIORITY_BUDGET_PRE == 10
    assert PRIORITY_TOOL_RBAC == 20
    assert PRIORITY_DATA_RBAC == 30
    assert PRIORITY_AUDIT == 80
    assert PRIORITY_BUDGET_POST == 90


def test_priority_canonical_order() -> None:
    """Stage B §C-2 canonical chain order: ascending priority."""
    chain = [
        PRIORITY_BUDGET_PRE,
        PRIORITY_TOOL_RBAC,
        PRIORITY_DATA_RBAC,
        PRIORITY_AUDIT,
        PRIORITY_BUDGET_POST,
    ]
    assert chain == sorted(chain)


def test_priority_reserved_gap() -> None:
    """Reserved gap 30..80 lets future PLANs slot in without renumbering."""
    assert PRIORITY_AUDIT - PRIORITY_DATA_RBAC >= 50
