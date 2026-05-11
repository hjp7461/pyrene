"""Startup wiring — register budget pre/post hooks at canonical priorities.

PLAN-014 Day 1. The host application calls `register_budget_hooks(...)`
once at boot:

```python
from pyrene_budget import BudgetAlerter, register_budget_hooks

alerter = BudgetAlerter(url=os.environ.get("BUDGET_ALERT_WEBHOOK"))
register_budget_hooks(
    gateway,
    session_factory=session_factory,
    summary_cache=summary_cache,
    alerter=alerter,
    audit_sink=audit_sink,  # optional — over_budget event signal
)
```

The function:
  1. Registers pre-hook at `PRIORITY_BUDGET_PRE = 10` (first in chain).
  2. Registers post-hook at `PRIORITY_BUDGET_POST = 90` (last in chain).

Returns the registered `(pre, post)` callables for test introspection.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyrene_budget.hooks import make_budget_post_hook, make_budget_pre_hook
from pyrene_budget.schemas import BudgetPeriod, BudgetScope
from pyrene_budget.webhook import BudgetAlerter
from pyrene_core.audit import AuditSink
from pyrene_gateway import (
    PRIORITY_BUDGET_POST,
    PRIORITY_BUDGET_PRE,
    AfterRunHook,
    BeforeRunHook,
    Gateway,
)
from pyrene_metering.aggregation import SummaryCache


def register_budget_hooks(
    gateway: Gateway,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    summary_cache: SummaryCache,
    alerter: BudgetAlerter,
    audit_sink: AuditSink | None = None,
    period: BudgetPeriod = "day",
    default_scope: BudgetScope = "user",
) -> tuple[BeforeRunHook, AfterRunHook]:
    """Wire both hooks onto the gateway.

    Returns the `(pre, post)` Protocol-compatible callables so callers
    can `assert pre in gateway.before_hooks()` in tests.
    """
    pre = make_budget_pre_hook(
        session_factory=session_factory,
        summary_cache=summary_cache,
        period=period,
        default_scope=default_scope,
    )
    post = make_budget_post_hook(
        session_factory=session_factory,
        summary_cache=summary_cache,
        alerter=alerter,
        audit_sink=audit_sink,
        period=period,
        default_scope=default_scope,
    )
    gateway.before_run(pre, priority=PRIORITY_BUDGET_PRE)
    gateway.after_run(post, priority=PRIORITY_BUDGET_POST)
    return pre, post


__all__ = ["register_budget_hooks"]
