"""Pyrene budget limits (PRD-014).

Phase 2 surface:
  - `BudgetLimit` ORM + `0008_budget_limits` migration.
  - `make_budget_pre_hook` / `make_budget_post_hook` factories.
  - `register_budget_hooks(gateway, ...)` startup helper — registers
    pre@PRIORITY_BUDGET_PRE (10) + post@PRIORITY_BUDGET_POST (90).
  - `BudgetAlerter` — 80/95/100 threshold webhook with TTL dedupe.
  - HTTP routes (`budgets_router`) + `register_exception_handlers(app)`
    for fail-closed HTTP mapping (PRD-014 L-01).

Wave 8 constraint: this package does NOT modify the
Gateway/agents/auth/metering — it imports `UsageSummary` /
`SummaryCache` from `pyrene_metering` (PRD-013 handoff) and registers
hooks via the public `Gateway.before_run` / `Gateway.after_run` API.
"""

from pyrene_budget.aggregation import remaining_budget, status_for
from pyrene_budget.errors import (
    BudgetError,
    BudgetExceededError,
    BudgetLockUnavailableError,
    BudgetSystemUnavailableError,
)
from pyrene_budget.hooks import make_budget_post_hook, make_budget_pre_hook
from pyrene_budget.models import Base, BudgetLimit, metadata
from pyrene_budget.repository import (
    delete_budget_limit,
    get_budget_limit,
    list_budget_limits,
    try_lock_for_scope,
    upsert_budget_limit,
)
from pyrene_budget.routes import (
    budgets_router,
    register_exception_handlers,
    set_summary_cache,
)
from pyrene_budget.schemas import (
    BudgetLimitCreate,
    BudgetLimitResponse,
    BudgetPeriod,
    BudgetScope,
    BudgetStatus,
)
from pyrene_budget.startup import register_budget_hooks
from pyrene_budget.webhook import DEFAULT_THRESHOLDS, BudgetAlerter

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_THRESHOLDS",
    "Base",
    "BudgetAlerter",
    "BudgetError",
    "BudgetExceededError",
    "BudgetLimit",
    "BudgetLimitCreate",
    "BudgetLimitResponse",
    "BudgetLockUnavailableError",
    "BudgetPeriod",
    "BudgetScope",
    "BudgetStatus",
    "BudgetSystemUnavailableError",
    "budgets_router",
    "delete_budget_limit",
    "get_budget_limit",
    "list_budget_limits",
    "make_budget_post_hook",
    "make_budget_pre_hook",
    "metadata",
    "register_budget_hooks",
    "register_exception_handlers",
    "remaining_budget",
    "set_summary_cache",
    "status_for",
    "try_lock_for_scope",
    "upsert_budget_limit",
]
