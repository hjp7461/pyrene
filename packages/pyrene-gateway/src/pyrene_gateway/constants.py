"""Hook priority constants — canonical 5-stage hook chain.

PRD-009 §C-2 / PLAN-009 Day 3 fix the priority numbers so PLAN-010,
PLAN-011, PLAN-013, PLAN-014, PLAN-015 can register hooks against a
shared schedule without coupling to each other.

Execution order: ascending priority. For `before_run` AND `after_run`,
lower priority runs first (symmetric, no reverse — Stage B §C-2).
Insertion order breaks ties (Python `list.sort` stable sort).

```
                                                       (tool execution
PRIORITY_BUDGET_PRE = 10                                 between
PRIORITY_TOOL_RBAC  = 20                                 before_run and
PRIORITY_DATA_RBAC  = 30                                 after_run)
   <tool runs>
PRIORITY_AUDIT       = 80
PRIORITY_BUDGET_POST = 90
```

Owning plan:
  - 10 / 90  : PLAN-014 (budget) — pre-flight check + post-flight charge
  - 20       : PLAN-010 (tool-level RBAC)
  - 30       : PLAN-011 (data-level RBAC)
  - 80       : PLAN-015 (audit emit)

Reserved gap (40..70) is intentional — new hook categories slot in
without renumbering. PLAN authors MUST NOT introduce ad-hoc values
without amending this module.
"""

from __future__ import annotations

PRIORITY_BUDGET_PRE: int = 10
PRIORITY_TOOL_RBAC: int = 20
PRIORITY_DATA_RBAC: int = 30
PRIORITY_AUDIT: int = 80
PRIORITY_BUDGET_POST: int = 90


__all__ = [
    "PRIORITY_AUDIT",
    "PRIORITY_BUDGET_POST",
    "PRIORITY_BUDGET_PRE",
    "PRIORITY_DATA_RBAC",
    "PRIORITY_TOOL_RBAC",
]
