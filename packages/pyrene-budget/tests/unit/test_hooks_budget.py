"""Unit tests for budget pre/post hook factories.

DB is mocked — these tests verify:
  - `make_budget_pre_hook` returns a `BeforeRunHook` Protocol-compatible callable.
  - `make_budget_post_hook` returns an `AfterRunHook` Protocol-compatible callable.
  - Both register at the canonical priorities (10, 90) via `register_budget_hooks`.
  - Lock-miss path raises `BudgetLockUnavailableError` (fail-closed).
  - Over-limit path raises `BudgetExceededError`.
  - Predicted-cost extraction handles missing / malformed metadata.
  - Webhook alerter dedupes by `(scope_id, period_label, threshold)`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from pyrene_budget import (
    BudgetAlerter,
    BudgetExceededError,
    BudgetLockUnavailableError,
    make_budget_post_hook,
    make_budget_pre_hook,
    register_budget_hooks,
)
from pyrene_budget.hooks import (
    _BUDGET_TO_METERING,
    _predicted_cost_from_ctx,
    _scope_for_ctx,
)
from pyrene_budget.repository import _composite_key
from pyrene_core import UserContext
from pyrene_gateway import (
    PRIORITY_BUDGET_POST,
    PRIORITY_BUDGET_PRE,
    AfterRunHook,
    BeforeRunHook,
    Gateway,
    RunContext,
)


def _ctx(metadata: dict[str, Any] | None = None) -> RunContext:
    return RunContext(
        user_context=UserContext(
            user_id=uuid4(), team_id=uuid4(), roles=("analyst",)
        ),
        request_id=uuid4(),
        agent_id=uuid4(),
        metadata=metadata or {},
    )


# --- pure helpers ---------------------------------------------------------


def test_predicted_cost_default_zero() -> None:
    assert _predicted_cost_from_ctx(_ctx()) == Decimal("0")


def test_predicted_cost_decimal_passthrough() -> None:
    ctx = _ctx({"predicted_cost_usd": Decimal("0.025")})
    assert _predicted_cost_from_ctx(ctx) == Decimal("0.025")


def test_predicted_cost_string_coerced() -> None:
    ctx = _ctx({"predicted_cost_usd": "1.5"})
    assert _predicted_cost_from_ctx(ctx) == Decimal("1.5")


def test_predicted_cost_garbage_falls_back_to_zero() -> None:
    ctx = _ctx({"predicted_cost_usd": "not-a-number"})
    assert _predicted_cost_from_ctx(ctx) == Decimal("0")


def test_scope_default_user() -> None:
    ctx = _ctx()
    scope, scope_id = _scope_for_ctx(ctx, default_scope="user")
    assert scope == "user"
    assert scope_id == ctx.user_context.user_id


def test_scope_default_team() -> None:
    ctx = _ctx()
    scope, scope_id = _scope_for_ctx(ctx, default_scope="team")
    assert scope == "team"
    assert scope_id == ctx.user_context.team_id


def test_scope_metadata_override() -> None:
    override_id = uuid4()
    ctx = _ctx({"budget_scope": "team", "budget_scope_id": override_id})
    scope, scope_id = _scope_for_ctx(ctx, default_scope="user")
    assert scope == "team"
    assert scope_id == override_id


def test_composite_key_canonical_shape() -> None:
    """The advisory-lock input string matches `scope:scope_id:period`."""
    scope_id = uuid4()
    key = _composite_key("user", scope_id, "day")
    assert key == f"user:{scope_id}:day"


def test_period_mapping_covers_set() -> None:
    """All three budget periods route to the same metering periods."""
    assert _BUDGET_TO_METERING == {"day": "day", "week": "week", "month": "month"}


# --- factory + protocol -------------------------------------------------


def test_pre_hook_satisfies_protocol() -> None:
    hook = make_budget_pre_hook(
        session_factory=MagicMock(),
        summary_cache=MagicMock(),
    )
    assert isinstance(hook, BeforeRunHook)


def test_post_hook_satisfies_protocol() -> None:
    hook = make_budget_post_hook(
        session_factory=MagicMock(),
        summary_cache=MagicMock(),
        alerter=BudgetAlerter(url=None),
    )
    assert isinstance(hook, AfterRunHook)


# --- priority registration (PRD-014 Hook 등록) --------------------------


def test_register_budget_hooks_priorities() -> None:
    """Pre at 10, Post at 90 (PRIORITY_BUDGET_PRE / PRIORITY_BUDGET_POST)."""
    gateway = Gateway()
    pre, post = register_budget_hooks(
        gateway,
        session_factory=MagicMock(),
        summary_cache=MagicMock(),
        alerter=BudgetAlerter(url=None),
    )
    assert PRIORITY_BUDGET_PRE == 10
    assert PRIORITY_BUDGET_POST == 90
    assert pre in gateway.before_hooks()
    assert post in gateway.after_hooks()
    assert gateway.before_hooks()[0] is pre  # priority 10 is first.
    assert gateway.after_hooks()[-1] is post  # priority 90 runs last.


def test_pre_runs_before_other_hooks_via_priority() -> None:
    """Two before-hooks: budget-pre @10 runs before another @20."""
    gateway = Gateway()
    register_budget_hooks(
        gateway,
        session_factory=MagicMock(),
        summary_cache=MagicMock(),
        alerter=BudgetAlerter(url=None),
    )

    async def other(ctx: RunContext) -> None:
        return None

    gateway.before_run(other, priority=20)
    before = gateway.before_hooks()
    assert len(before) == 2
    # Index 0 is budget pre @10; index 1 is `other` @20.
    assert before[1] is other


# --- pre-hook: lock-miss path (fail-closed) ----------------------------


async def test_pre_hook_lock_miss_raises_lock_unavailable() -> None:
    """`pg_try_advisory_xact_lock` returning false → BudgetLockUnavailableError."""
    # Mock session returning False on the advisory lock SELECT.
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    result = MagicMock()
    result.scalar_one.return_value = False
    session.execute.return_value = result

    factory = MagicMock(return_value=session)

    hook = make_budget_pre_hook(
        session_factory=factory,
        summary_cache=MagicMock(),
    )
    with pytest.raises(BudgetLockUnavailableError):
        await hook(_ctx())


# --- pre-hook: gate path (over-budget) --------------------------------


async def test_pre_hook_over_budget_raises_exceeded() -> None:
    """used + predicted >= limit → BudgetExceededError."""
    # Lock SELECT returns True; lookup returns a row with limit=$1.
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    limit_row_result = MagicMock()
    limit_row = MagicMock()
    limit_row.limit_usd = Decimal("1.00")
    limit_row_result.scalar_one_or_none.return_value = limit_row

    # First execute: advisory lock; second: SELECT BudgetLimit.
    session.execute.side_effect = [lock_result, limit_row_result]

    summary = MagicMock()
    summary.total_cost_usd = Decimal("0.99")
    summary_cache = MagicMock()
    summary_cache.by_user = AsyncMock(return_value=summary)

    hook = make_budget_pre_hook(
        session_factory=MagicMock(return_value=session),
        summary_cache=summary_cache,
    )
    # predicted=$0.05 → 0.99 + 0.05 = 1.04 >= 1.00 → blocked.
    with pytest.raises(BudgetExceededError) as exc_info:
        await hook(_ctx({"predicted_cost_usd": Decimal("0.05")}))
    assert exc_info.value.limit_usd == Decimal("1.00")
    assert exc_info.value.used_usd == Decimal("0.99")
    assert exc_info.value.predicted_usd == Decimal("0.05")


async def test_pre_hook_under_budget_passes() -> None:
    """used + predicted < limit → no exception, projection stamped."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    limit_row_result = MagicMock()
    limit_row = MagicMock()
    limit_row.limit_usd = Decimal("5.00")
    limit_row_result.scalar_one_or_none.return_value = limit_row
    session.execute.side_effect = [lock_result, limit_row_result]

    summary = MagicMock()
    summary.total_cost_usd = Decimal("1.00")
    summary_cache = MagicMock()
    summary_cache.by_user = AsyncMock(return_value=summary)

    hook = make_budget_pre_hook(
        session_factory=MagicMock(return_value=session),
        summary_cache=summary_cache,
    )
    ctx = _ctx({"predicted_cost_usd": Decimal("0.05")})
    await hook(ctx)  # no raise
    assert "budget_projection" in ctx.metadata
    assert ctx.metadata["budget_projection"]["limit_usd"] == Decimal("5.00")


async def test_pre_hook_no_limit_passes_silently() -> None:
    """No `BudgetLimit` row → hook passes (host-policy fallback)."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    limit_row_result = MagicMock()
    limit_row_result.scalar_one_or_none.return_value = None  # no budget
    session.execute.side_effect = [lock_result, limit_row_result]

    hook = make_budget_pre_hook(
        session_factory=MagicMock(return_value=session),
        summary_cache=MagicMock(),
    )
    await hook(_ctx())  # no raise, summary_cache never consulted.


# --- alerter dedupe (80/95/100 webhook semantics) ----------------------


async def test_alerter_fires_once_per_threshold() -> None:
    """The 80% threshold must fire exactly once per (period_label, scope_id)."""
    posts: list[tuple[str, dict[str, Any]]] = []

    async def stub_poster(url: str, payload: dict[str, Any]) -> None:
        posts.append((url, payload))

    alerter = BudgetAlerter(url="http://t/", poster=stub_poster)
    sid = uuid4()
    label = "2026-05-11"

    # First crossing at 82% — 80% fires.
    fired_1 = await alerter.maybe_fire(
        scope="user",
        scope_id=sid,
        period="day",
        period_label=label,
        used_usd=Decimal("0.82"),
        limit_usd=Decimal("1.00"),
        used_pct=Decimal("82.00"),
    )
    assert fired_1 == Decimal("80")
    assert len(posts) == 1

    # Second crossing same day at 83% — same threshold, dedupe suppresses.
    fired_2 = await alerter.maybe_fire(
        scope="user",
        scope_id=sid,
        period="day",
        period_label=label,
        used_usd=Decimal("0.83"),
        limit_usd=Decimal("1.00"),
        used_pct=Decimal("83.00"),
    )
    assert fired_2 is None
    assert len(posts) == 1


async def test_alerter_advances_to_higher_thresholds() -> None:
    """Crossing 95% after 80% fires the 95% webhook (a *new* threshold)."""
    posts: list[tuple[str, dict[str, Any]]] = []

    async def stub_poster(url: str, payload: dict[str, Any]) -> None:
        posts.append((url, payload))

    alerter = BudgetAlerter(url="http://t/", poster=stub_poster)
    sid = uuid4()
    label = "2026-05-11"

    # 80% crossing.
    await alerter.maybe_fire(
        scope="user", scope_id=sid, period="day", period_label=label,
        used_usd=Decimal("0.85"), limit_usd=Decimal("1.00"),
        used_pct=Decimal("85.00"),
    )
    # 95% crossing.
    fired = await alerter.maybe_fire(
        scope="user", scope_id=sid, period="day", period_label=label,
        used_usd=Decimal("0.96"), limit_usd=Decimal("1.00"),
        used_pct=Decimal("96.00"),
    )
    assert fired == Decimal("95")
    assert len(posts) == 2
    # 100% crossing.
    fired_100 = await alerter.maybe_fire(
        scope="user", scope_id=sid, period="day", period_label=label,
        used_usd=Decimal("1.05"), limit_usd=Decimal("1.00"),
        used_pct=Decimal("105.00"),
    )
    assert fired_100 == Decimal("100")
    assert len(posts) == 3


async def test_alerter_resets_on_new_period_label() -> None:
    """Crossing 80% in a new bucket fires again (dedupe scoped to period_label)."""
    posts: list[tuple[str, dict[str, Any]]] = []

    async def stub_poster(url: str, payload: dict[str, Any]) -> None:
        posts.append((url, payload))

    alerter = BudgetAlerter(url="http://t/", poster=stub_poster)
    sid = uuid4()

    await alerter.maybe_fire(
        scope="user", scope_id=sid, period="day", period_label="2026-05-11",
        used_usd=Decimal("0.85"), limit_usd=Decimal("1.00"),
        used_pct=Decimal("85.00"),
    )
    assert len(posts) == 1
    # Different period_label → fresh dedupe key.
    fired = await alerter.maybe_fire(
        scope="user", scope_id=sid, period="day", period_label="2026-05-12",
        used_usd=Decimal("0.85"), limit_usd=Decimal("1.00"),
        used_pct=Decimal("85.00"),
    )
    assert fired == Decimal("80")
    assert len(posts) == 2


async def test_alerter_no_url_is_noop() -> None:
    """When `url=None` the alerter never posts (dev/test mode)."""
    alerter = BudgetAlerter(url=None)
    fired = await alerter.maybe_fire(
        scope="user", scope_id=uuid4(), period="day", period_label="L",
        used_usd=Decimal("1.0"), limit_usd=Decimal("1.0"),
        used_pct=Decimal("100"),
    )
    assert fired is None


def test_alerter_thresholds_must_ascend() -> None:
    """Constructor rejects descending threshold lists (sign of a bug)."""
    with pytest.raises(ValueError, match="ascending"):
        BudgetAlerter(
            url=None,
            thresholds=(Decimal("95"), Decimal("80")),
        )


async def test_alerter_webhook_failure_silent_drop() -> None:
    """POST failure must not raise — PRD-014 §위험 신호 #4."""

    async def failing_poster(url: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("network down")

    alerter = BudgetAlerter(url="http://t/", poster=failing_poster)
    # Must not raise.
    fired = await alerter.maybe_fire(
        scope="user", scope_id=uuid4(), period="day", period_label="L",
        used_usd=Decimal("1.0"), limit_usd=Decimal("1.0"),
        used_pct=Decimal("100"),
    )
    # Threshold still recorded (dedupe stamped before post attempt) — see docstring.
    assert fired == Decimal("100")
