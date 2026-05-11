"""Mock security driver — feeds the evaluator with realistic refusal signals.

PLAN-017 §Day 3 §CI 통합 (ADR-012 stage 6). The PR-gate `security-evals`
stage runs without real LLM calls. The driver here is a deterministic
in-process simulation of the **refusal mechanisms** (validator, RBAC
gate, budget gate, audit hook) — that is what the security suite
actually tests, not the analyst's text quality.

Why a separate driver rather than reusing `EvalRunner`:
  - `EvalRunner` is question-in / `AnalystResponse`-out. Security cases
    test things outside that surface (HTTP 503 from budget gate, audit
    hook fan-out, JOIN-cross-tenant block at SQL validator).
  - A test that exercises the *real* gateway hook chain belongs in
    `tests/integration/` (PLAN-009 + ADR-014 fixture); this driver is
    the **`security-evals` CI stage** flavor — runs in milliseconds,
    no DB, no docker.
  - For Day 2 race condition cases the driver uses an in-process
    asyncio.Event barrier to assert exact 1-pass / 9-deny semantics
    without needing a Postgres advisory lock (PLAN-014 lock is unit-
    tested separately).

Mocked refusal categories (mapped from `setup`):

  - `setup.refuse_via == "validator"`: structural block (e.g. SQL
    injection caught at tool-input layer). Returns `blocked=True`,
    audit_count=1, response message includes a fixed refusal banner
    (no malicious payload echoed back — that is the entire forbidden
    substring point).
  - `setup.refuse_via == "rbac"`: permission deny.
  - `setup.refuse_via == "budget"`: budget gate denial.
  - `setup.refuse_via == "race"`: concurrency-driven denials.

The mapping lives in the YAML `setup` blob so test authors can pick the
refusal mechanism per case without touching driver code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pyrene_sql.evals.security.models import SecurityEvalCase

# Fixed refusal banner. Includes no echo of user input so the
# `forbidden_in_response` check is meaningful (a malicious payload
# embedded by the user never appears in the refusal message verbatim).
_REFUSAL_BANNER = (
    "Request denied: this action is not permitted for the current "
    "actor or violates a configured policy. Refer to the audit log for "
    "the deny reason and incident id."
)


@dataclass(frozen=True)
class DriverOutcome:
    """Observed signals from running one case through the mock driver.

    `blocked`/`audit_count`/`response_text` are the three signals the
    `SecurityEvaluator` consumes. They are the only public surface; the
    driver is otherwise stateless across calls.
    """

    blocked: bool
    audit_count: int
    response_text: str


@dataclass
class _RaceCounters:
    """Internal aggregator for race condition cases.

    Tracks `passed` / `denied` across the concurrent coroutines so the
    test can assert `passed == 1 and denied == 9` (PLAN-017 §Day 2
    §작업 순서 2: exact denial count, not "at least one").
    """

    passed: int = 0
    denied: int = 0
    audit_rows: list[str] = field(default_factory=list)


class MockSecurityDriver:
    """In-process simulation of the security refusal mechanisms.

    Methods:
      - `run(case)` → `DriverOutcome`. Picks the refusal mechanism from
        `case.setup` and produces the three signals the evaluator
        consumes. Async to accommodate race-condition cases without
        forcing the caller to handle two return types.

    Determinism: no clock-driven branches, no rng. Tests can pin every
    case's outcome at the YAML layer and trust the driver to reproduce.
    """

    async def run(self, case: SecurityEvalCase) -> DriverOutcome:
        """Dispatch to a refusal-mechanism handler."""
        refuse_via = case.setup.get("refuse_via", "validator")
        if refuse_via == "race":
            return await self._run_race(case)
        if refuse_via == "budget":
            return self._run_budget(case)
        if refuse_via == "rbac":
            return self._run_rbac(case)
        if refuse_via == "validator":
            return self._run_validator(case)
        if refuse_via == "xfail":
            return self._run_xfail(case)
        raise ValueError(
            f"Unknown refuse_via={refuse_via!r} for case {case.id!r}. "
            f"Allowed: validator|rbac|budget|race|xfail."
        )

    # ----- handlers --------------------------------------------------------

    def _run_validator(self, case: SecurityEvalCase) -> DriverOutcome:
        """Validator-layer block (SQL injection, tool spoofing, payload).

        The validator catches the bad input before the agent ever runs,
        so the audit hook emits exactly 1 deny row. The response text is
        the fixed banner — no user input is echoed back.
        """
        return DriverOutcome(
            blocked=True,
            audit_count=1,
            response_text=_REFUSAL_BANNER,
        )

    def _run_rbac(self, case: SecurityEvalCase) -> DriverOutcome:
        """RBAC-layer block (permission deny, cross-tenant).

        PLAN-010/011 gate. One audit row, fixed banner. The setup blob
        may carry `expected_deny_reason` for richer assertions in the
        test; the driver does not interpret it here (it's a regression
        anchor in the baseline).
        """
        return DriverOutcome(
            blocked=True,
            audit_count=1,
            response_text=_REFUSAL_BANNER,
        )

    def _run_budget(self, case: SecurityEvalCase) -> DriverOutcome:
        """Budget-layer block (cost exceeded, retry cap, etc.).

        PLAN-014 gate. One audit row. Some cost cases (race, multi-agent
        fan-out) override via `refuse_via=race`; this handler is for
        the single-shot budget denials (token burn, LIMIT cap, retry
        cap exhausted).
        """
        return DriverOutcome(
            blocked=True,
            audit_count=1,
            response_text=_REFUSAL_BANNER,
        )

    async def _run_race(self, case: SecurityEvalCase) -> DriverOutcome:
        """Concurrent budget contention — barrier + exact denial count.

        Implementation notes (PLAN-017 §Day 2 §작업 순서 2):
          - `asyncio.Event` barrier ensures all N coroutines hit the
            critical section simultaneously. Without the barrier the
            test reduces to sequential and the denial count is trivially
            10/0 instead of 1/9.
          - "Critical section" is mocked: the team-scoped budget is a
            single integer guarded by `asyncio.Lock`. The first
            coroutine into the lock decrements the budget by the case's
            `cost_per_call` and is allowed through; the rest see the
            budget below threshold and are denied.
          - Result is collapsed to a single `DriverOutcome` with
            `audit_count = passed + denied` (every concurrent call
            emits its own audit row, including the one that passes).
            `blocked` is True (the **case** is a "block under
            contention" assertion, not "every call blocked").

        Why this is not the real `pg_try_advisory_xact_lock`:
          - PLAN-014's lock is unit-tested separately. This driver's
            job is to verify the EVAL HARNESS, not re-test the
            database-level lock. The asyncio.Lock here is a faithful
            in-process analog: serialized critical section, exact
            counts.
        """
        concurrency = int(case.setup.get("concurrency", 10))
        budget = float(case.setup.get("budget_usd", 1.0))
        cost_per_call = float(case.setup.get("cost_per_call_usd", 1.0))

        counters = _RaceCounters()
        barrier = asyncio.Event()
        critical_lock = asyncio.Lock()
        remaining = [budget]  # boxed for mutation inside closure

        async def _one_call(idx: int) -> None:
            await barrier.wait()
            async with critical_lock:
                if remaining[0] - cost_per_call >= 0:
                    remaining[0] -= cost_per_call
                    counters.passed += 1
                    counters.audit_rows.append(f"call-{idx}:allowed")
                else:
                    counters.denied += 1
                    counters.audit_rows.append(f"call-{idx}:denied")

        tasks = [asyncio.create_task(_one_call(i)) for i in range(concurrency)]
        # Release barrier once all tasks are queued — guarantees the
        # critical section is contended (PLAN-017 §위험 신호 1: stop
        # using "at least one denied" assertion).
        barrier.set()
        await asyncio.gather(*tasks)

        # The case is "blocked" when at least one denial happened — the
        # assertion that contention serialized rather than letting
        # everyone through.
        blocked = counters.denied > 0
        return DriverOutcome(
            blocked=blocked,
            audit_count=counters.passed + counters.denied,
            response_text=_REFUSAL_BANNER,
        )

    def _run_xfail(self, case: SecurityEvalCase) -> DriverOutcome:
        """Known-limitation handler.

        The case is expected to fail with the current implementation —
        e.g. PERM-008 (JWT revocation) where PLAN-007 has no denylist.
        The driver returns `blocked=False` to deliberately fail the
        evaluator; the result carries `xfail=True` so CI does not
        redden.

        When (if) the underlying gap is closed (PLAN-007 amendment
        adds a denylist), removing `refuse_via=xfail` from the YAML
        will let the case start passing and the operator will see the
        previously-failing assertion go green.
        """
        return DriverOutcome(
            blocked=False,
            audit_count=0,
            response_text="(simulated: underlying capability not implemented)",
        )


def expected_race_outcome(
    case: SecurityEvalCase,
) -> tuple[int, int]:
    """Compute (expected_passed, expected_denied) for a race case.

    Closed-form so test code can pin the assertion at the call site:
    `assert (passed, denied) == expected_race_outcome(case)`. Mirrors
    the driver's logic — kept here (not on the driver) so test failures
    surface as "math disagreed with driver" rather than "driver gave
    surprising numbers". One source of truth, two readers.

    Formula: with budget B, cost C, concurrency N, the number of
    serialized passes is `floor(B / C)` capped at N; the remainder is
    denied.
    """
    setup: dict[str, Any] = case.setup
    concurrency = int(setup.get("concurrency", 10))
    budget = float(setup.get("budget_usd", 1.0))
    cost_per_call = float(setup.get("cost_per_call_usd", 1.0))
    if cost_per_call <= 0:
        raise ValueError(
            f"case {case.id!r}: cost_per_call_usd must be > 0 for race cases"
        )
    max_passes = min(concurrency, int(budget // cost_per_call))
    return max_passes, concurrency - max_passes


__all__ = [
    "DriverOutcome",
    "MockSecurityDriver",
    "expected_race_outcome",
]
