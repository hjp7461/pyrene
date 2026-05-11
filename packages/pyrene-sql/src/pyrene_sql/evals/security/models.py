"""Pydantic models for the security evals harness.

PLAN-017 §4 / PRD-017 §4. Three top-level shapes:

- `SecurityEvalCase` — one entry from a category YAML. Carries the
  malicious-or-mistaken input plus the deterministic expectations the
  evaluator will assert against. Sibling of `EvalCase` (PLAN-005 §1) but
  separate type because the **expectations** are different (`must_block`,
  `must_audit`, `forbidden_in_response`) rather than SQL keyword / row
  count matches.
- `SecurityEvalExpectation` — the deterministic gate. `must_block=True`
  asserts the system refused the request; `must_audit=True` asserts an
  audit row was emitted (PLAN-015 hot path); `forbidden_in_response` is a
  tuple of substrings that must NOT appear in the response text (PII /
  SQL fragment / cross-tenant leak signals).
- `SecurityEvalResult` — the evaluator's verdict per case, carrying the
  observed signals (`audit_count`, `blocked`, `response_text`) so the
  baseline snapshot can pin them and surface regressions.

Why a separate module instead of extending `evals/models.py`:
  - The security evaluator is **deterministic only** — no judge model,
    no fuzzy thresholds. The shape needs `must_*` boolean gates and an
    integer audit count, which would dilute the meaning of
    `EvalCase.expected_*` if mashed together.
  - PLAN-005's `EvalCase` carries `expected_sql_keywords` etc. tied to
    `AnalystResponse` — irrelevant here because security cases test
    refusal mechanisms (RBAC, budget, hook chain) rather than the SQL
    itself.
  - Independent evolution: PLAN-017 is allowed to add `xfail_reason`,
    `setup`, and category-specific keys without touching PLAN-005's
    schema (Stage B lock-step agreement).

Frozen + `extra="forbid"` via `StrictBaseModel` per BRIEF §6.1-1: a typo
in `must_audt` would silently default to False otherwise; the loader fails
in CI instead.
"""

from __future__ import annotations

from typing import Any, Literal

from pyrene_core import StrictBaseModel

SecurityCategory = Literal["bypass", "cost", "permission"]
"""Closed set — the three datasets named in PRD-017 §2.

Adding a fourth category is a schema migration (new YAML file + new
baseline directory + CI matrix expansion). Keeping the literal closed
makes the matrix fail-fast on typos.
"""


class SecurityEvalExpectation(StrictBaseModel):
    """Deterministic gate for a single security case.

    Three fields are the **main signal** (per PLAN-017 §Day 1 completion
    criteria: `must_block=true AND must_audit_count==1 AND
    forbidden_in_response empty`):

    - `must_block`: True means the system MUST refuse the request. The
      evaluator asserts the response carries a refusal marker (e.g.
      `AnalystResponse.refusal is not None`, or a 4xx/5xx HTTP status,
      or a `BudgetExceededError` raised). False is allowed but rare —
      only when a case validates a positive path (e.g. permission case
      that admin SHOULD be able to do).
    - `must_audit_count`: exact number of audit rows expected to be
      emitted by this case. Default 1 — most cases produce exactly one
      deny row. 0 is allowed for cases that exercise the validator
      layer (pre-RBAC, structural rejection) where the audit hook never
      fires. Larger values cover cases that fan out (race condition: 10
      concurrent → 10 audit rows).
    - `forbidden_in_response`: substrings that must NOT appear in the
      response text. Regression net for information leakage — e.g. if
      the SQL fragment `DROP TABLE` shows up in a refusal message, the
      system is leaking the malicious payload back to the user. Tuples
      (frozen) so callers cannot mutate the expectation post-load.

    Optional fields:
    - `xfail_reason`: when set, the evaluator marks the case as expected
      to fail and records the reason. Used for known limitations like
      "JWT revocation not implemented in PLAN-007" — the case still
      runs (we want to catch the day it suddenly passes) but a fail
      does not turn CI red.
    - `cost_limit_usd`: only meaningful for cost cases — asserts the
      cumulative spend stays below this cap even under the malicious
      input. None disables the check.
    """

    must_block: bool = True
    must_audit_count: int = 1
    forbidden_in_response: tuple[str, ...] = ()
    xfail_reason: str | None = None
    cost_limit_usd: float | None = None


class SecurityEvalCase(StrictBaseModel):
    """One malicious-or-mistaken input with deterministic expectations.

    Field rationale:
      - `id`: unique within (and across) categories. Prefix convention:
        `BYP-NNN` / `COST-NNN` / `PERM-NNN`. The prefix is redundant
        with `category` but makes log lines self-describing.
      - `category`: which YAML this case lives in. Sole purpose is to
        let downstream code shard the run by category without parsing
        the id prefix.
      - `setup`: opaque dict for case-specific setup directives —
        - bypass: `{"actor_role": "analyst", "table_blocked": "payment"}`
        - cost: `{"budget_usd": 10.0, "concurrency": 10}`
        - permission: `{"actor_role": "viewer", "target_team_id": "..."}`
        Kept as `dict[str, Any]` deliberately because the setup keys are
        category-specific and a typed union would explode combinatorially.
        The evaluator (or the test that consumes the case) is responsible
        for asserting on keys it cares about.
      - `input`: the natural-language prompt OR an API call descriptor.
        Strings are the common case (most bypass / permission cases are
        prompts); cost / race cases use API descriptors (JSON-stringified
        in YAML).
      - `expected`: the deterministic gate. Defaulted to a baseline
        expectation (must_block + 1 audit row) so simple YAML entries
        stay short.
      - `description`: human-readable summary surfaced in failure logs.
        Not a docstring substitute — the YAML file's comments carry the
        category-level rationale.

    Why `setup` is `dict[str, Any]` and not a strict typed union:
      - Each category needs different setup keys (RBAC matrix vs budget
        cap vs concurrency level). A typed union would force the loader
        to know all three shapes; right now the loader only validates
        the top-level case + expectation, and the per-test fixture does
        the category-specific assertion.
      - Per the project-wide invariant (BRIEF §6.1-1) `dict[str, Any]`
        is a last resort, but this is the case it was designed for —
        opaque test-fixture state that does not flow through any
        production model.
    """

    id: str
    category: SecurityCategory
    description: str
    setup: dict[str, Any]
    input: str
    expected: SecurityEvalExpectation = SecurityEvalExpectation()


class SecurityEvalResult(StrictBaseModel):
    """Evaluator verdict for one `SecurityEvalCase`.

    Captured signals (vs the expectation):
      - `blocked`: did the case-under-test actually refuse?
      - `audit_count`: number of audit rows the system emitted during
        the case run. The evaluator counts via the test-scoped audit
        sink (a list-backed in-memory sink per ADR-014 fixture).
      - `response_text`: full response text (refusal message or SQL),
        kept verbatim for baseline diffing.
      - `passed`: deterministic AND of (blocked == must_block,
        audit_count == must_audit_count, no forbidden substring in
        response_text). The evaluator does not have to be clever —
        every check is a boolean.
      - `xfail`: True when the case was marked as a known limitation
        (`expected.xfail_reason` set). xfail cases never turn the CI
        red regardless of `passed`.
      - `failure_reasons`: tuple of human strings explaining each
        failed check. Empty when `passed`. Pinned in the baseline so
        regressions surface "the case stopped failing for the same
        reason" — that is itself a signal.

    The `actual_audit_count == expected.must_audit_count` check is
    strict equality (not `>=`) because a fan-out from a single denied
    request indicates a different bug (e.g. the audit hook running
    twice). The race-condition cases set `must_audit_count` to 10
    explicitly.
    """

    case_id: str
    passed: bool
    blocked: bool
    audit_count: int
    response_text: str
    xfail: bool = False
    failure_reasons: tuple[str, ...] = ()


__all__ = [
    "SecurityCategory",
    "SecurityEvalCase",
    "SecurityEvalExpectation",
    "SecurityEvalResult",
]
