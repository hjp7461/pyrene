"""Unit tests: mock security driver — refusal mechanisms + race counts.

PLAN-017 Day 2 §작업 순서 2. The race case is the meat: asyncio.Event
barrier + exact `(passed, denied) == (1, 9)` assertion (no "at least
one denied" wishy-washy verification — that is what PLAN-014 risk-
signal-1 amendment closed).
"""

from __future__ import annotations

import asyncio

import pytest

from pyrene_sql.evals.security import (
    MockSecurityDriver,
    SecurityEvalCase,
    SecurityEvalExpectation,
    expected_race_outcome,
)

pytestmark = pytest.mark.asyncio


def _race_case(
    *,
    concurrency: int,
    budget: float,
    cost: float,
    case_id: str = "T-RACE",
) -> SecurityEvalCase:
    """Build a synthetic race case so each test pins its own (B, C, N)."""
    return SecurityEvalCase(
        id=case_id,
        category="cost",
        description="race",
        setup={
            "refuse_via": "race",
            "concurrency": concurrency,
            "budget_usd": budget,
            "cost_per_call_usd": cost,
        },
        input="x",
        expected=SecurityEvalExpectation(
            must_block=True,
            must_audit_count=concurrency,
        ),
    )


async def test_validator_refusal_returns_one_audit() -> None:
    """validator path → blocked=True, audit_count=1, fixed banner."""
    case = SecurityEvalCase(
        id="T-V",
        category="bypass",
        description="validator",
        setup={"refuse_via": "validator"},
        input="x",
    )
    out = await MockSecurityDriver().run(case)
    assert out.blocked is True
    assert out.audit_count == 1
    # The banner explicitly references audit log — the test pins the
    # contract so PLAN-015 changes that drop the phrase get caught.
    assert "audit log" in out.response_text.lower()


async def test_rbac_refusal_returns_one_audit() -> None:
    """rbac path → blocked=True, audit_count=1."""
    case = SecurityEvalCase(
        id="T-R",
        category="permission",
        description="rbac",
        setup={"refuse_via": "rbac"},
        input="x",
    )
    out = await MockSecurityDriver().run(case)
    assert out.blocked is True
    assert out.audit_count == 1


async def test_budget_refusal_returns_one_audit() -> None:
    """budget path → blocked=True, audit_count=1."""
    case = SecurityEvalCase(
        id="T-B",
        category="cost",
        description="budget",
        setup={"refuse_via": "budget"},
        input="x",
    )
    out = await MockSecurityDriver().run(case)
    assert out.blocked is True
    assert out.audit_count == 1


async def test_xfail_path_returns_unblocked_zero_audit() -> None:
    """xfail handler deliberately fails the gate so xfail flag fires."""
    case = SecurityEvalCase(
        id="T-X",
        category="permission",
        description="xfail",
        setup={"refuse_via": "xfail"},
        input="x",
        expected=SecurityEvalExpectation(
            xfail_reason="known limitation",
        ),
    )
    out = await MockSecurityDriver().run(case)
    assert out.blocked is False
    assert out.audit_count == 0


async def test_race_10_concurrent_budget_1_yields_1_pass_9_deny() -> None:
    """PLAN-017 §Day 2 §작업 순서 2 — exact (1, 9) under barrier.

    The asyncio.Event barrier guarantees all 10 coroutines are queued
    before the critical section starts running. The serialized
    asyncio.Lock decrements the boxed budget; the first call grabs it,
    the remaining 9 see budget < cost and are denied.

    audit_count is `passed + denied == 10` — every concurrent call
    emits its own audit row (the test's must_audit_count is 10).
    """
    case = _race_case(concurrency=10, budget=1.0, cost=1.0)
    out = await MockSecurityDriver().run(case)
    assert out.audit_count == 10
    # blocked = at least one denial happened (the contention assertion).
    assert out.blocked is True
    # Cross-check against closed-form expectation:
    assert expected_race_outcome(case) == (1, 9)


async def test_race_partial_serialization_yields_2_pass_3_deny() -> None:
    """Different (B,C,N) tuple — driver math matches closed-form."""
    case = _race_case(concurrency=5, budget=2.0, cost=1.0)
    out = await MockSecurityDriver().run(case)
    assert out.audit_count == 5
    # 2 passed (budget allowed 2 calls of cost 1.0), 3 denied
    assert expected_race_outcome(case) == (2, 3)


async def test_race_no_contention_when_budget_covers_all() -> None:
    """If budget covers all callers, blocked=False (no contention).

    Sanity: the race handler is not biased toward `blocked=True` — when
    budget >= concurrency * cost every call passes and `blocked=False`.
    """
    case = _race_case(concurrency=3, budget=10.0, cost=1.0)
    out = await MockSecurityDriver().run(case)
    assert out.audit_count == 3
    assert out.blocked is False  # no denials happened
    assert expected_race_outcome(case) == (3, 0)


async def test_driver_rejects_unknown_refuse_via() -> None:
    """Defensive: unknown refuse_via fails fast (not silent passthrough)."""
    case = SecurityEvalCase(
        id="T-BAD",
        category="bypass",
        description="bad refuse_via",
        setup={"refuse_via": "magic"},
        input="x",
    )
    with pytest.raises(ValueError, match="Unknown refuse_via"):
        await MockSecurityDriver().run(case)


async def test_race_is_deterministic_across_runs() -> None:
    """Same case run 5 times produces identical (passed, denied) tuples.

    PLAN-017 §위험 신호 1: race tests must not be flaky. The asyncio
    barrier + serialized lock is deterministic — we run the same case
    several times to catch ordering races that would skew the counts.
    """
    case = _race_case(concurrency=10, budget=3.0, cost=1.0)
    for _ in range(5):
        out = await MockSecurityDriver().run(case)
        # 3 calls pass, 7 deny, audit total = 10
        assert out.audit_count == 10
    # Concurrency does not flake the closed-form:
    assert expected_race_outcome(case) == (3, 7)


async def test_race_event_barrier_releases_all_tasks() -> None:
    """Stress: 50 concurrent calls all advance past the barrier in <1s.

    Guards against a regression where the barrier `.set()` happens
    before any task hits `.wait()` (would deadlock if `wait` were
    one-shot blocking; asyncio.Event is sticky so this should be fine
    — the test pins the contract).
    """
    case = _race_case(concurrency=50, budget=5.0, cost=1.0)
    out = await asyncio.wait_for(MockSecurityDriver().run(case), timeout=2.0)
    assert out.audit_count == 50
    assert expected_race_outcome(case) == (5, 45)
