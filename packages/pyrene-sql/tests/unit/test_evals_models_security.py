"""Unit tests: security eval models + loader + evaluator (deterministic).

PLAN-017 Wave 9 §Day 1. No DB, no docker, no LLM. Focus:
  - YAML loader produces 17 / 13 / 12 cases for each category
  - Evaluator's three deterministic checks all fire independently
  - `xfail_reason` correctly flips the result's xfail flag
  - `expected_race_outcome` matches the driver's serialized counts

Naming: every file in this PLAN's test surface ends in `_security` to
avoid colliding with PLAN-005 / PLAN-016 / Wave-9 sibling PLANs (per
the Wave 9 충돌 회피 directive).
"""

from __future__ import annotations

import pytest

from pyrene_sql.evals.security import (
    SecurityEvalCase,
    SecurityEvalExpectation,
    SecurityEvaluator,
    expected_race_outcome,
    load_all_security_datasets,
    load_security_dataset,
)

# All tests in this file are sync; mypy --strict tolerates either. We
# keep `asyncio` markers for the small set of cases that exercise the
# async driver (separate test file).


def test_load_bypass_dataset_yields_17_cases() -> None:
    """PLAN-017 Day 1 mandates 17 bypass cases."""
    cases = load_security_dataset("bypass")
    assert len(cases) == 17
    # IDs are unique within the dataset
    ids = [c.id for c in cases]
    assert len(set(ids)) == 17
    # Every case carries the right category literal (loader does not
    # silently rewrite the YAML value)
    assert all(c.category == "bypass" for c in cases)


def test_load_cost_dataset_yields_13_cases() -> None:
    """PLAN-017 Day 2 mandates 13 cost cases."""
    cases = load_security_dataset("cost")
    assert len(cases) == 13
    assert all(c.category == "cost" for c in cases)


def test_load_permission_dataset_yields_12_cases() -> None:
    """PLAN-017 Day 3 mandates 12 permission cases (1 xfail)."""
    cases = load_security_dataset("permission")
    assert len(cases) == 12
    assert all(c.category == "permission" for c in cases)
    # Exactly 1 xfail (PERM-010 / stale JWT) — see PLAN-017 Day 3
    xfails = [c for c in cases if c.expected.xfail_reason is not None]
    assert len(xfails) == 1
    assert xfails[0].id == "PERM-010"


def test_load_all_42_cases() -> None:
    """Aggregate smoke — 17+13+12=42 cases load in deterministic order."""
    cases = load_all_security_datasets()
    assert len(cases) == 42
    # Order: bypass, then cost, then permission (loader pins this)
    categories = [c.category for c in cases]
    assert categories[:17] == ["bypass"] * 17
    assert categories[17:30] == ["cost"] * 13
    assert categories[30:] == ["permission"] * 12


def test_evaluator_passes_when_all_three_signals_match() -> None:
    """Deterministic happy path — blocked=True, audit=1, no leakage."""
    case = SecurityEvalCase(
        id="T-001",
        category="bypass",
        description="happy path",
        setup={},
        input="x",
        expected=SecurityEvalExpectation(
            must_block=True,
            must_audit_count=1,
            forbidden_in_response=("DROP TABLE",),
        ),
    )
    ev = SecurityEvaluator()
    r = ev.evaluate(
        case, blocked=True, audit_count=1, response_text="Request denied."
    )
    assert r.passed is True
    assert r.failure_reasons == ()


def test_evaluator_flags_each_failure_independently() -> None:
    """All three checks fire — failure_reasons accumulates every miss."""
    case = SecurityEvalCase(
        id="T-002",
        category="bypass",
        description="fail all three",
        setup={},
        input="x",
        expected=SecurityEvalExpectation(
            must_block=True,
            must_audit_count=2,
            forbidden_in_response=("DROP TABLE",),
        ),
    )
    ev = SecurityEvaluator()
    r = ev.evaluate(
        case,
        blocked=False,
        audit_count=1,
        response_text="Oops we ran DROP TABLE rental",
    )
    assert r.passed is False
    # Three reasons, one per failed check
    assert len(r.failure_reasons) == 3
    assert any("blocked" in s for s in r.failure_reasons)
    assert any("audit_count" in s for s in r.failure_reasons)
    assert any("forbidden substring" in s for s in r.failure_reasons)


def test_evaluator_forbidden_match_is_case_insensitive() -> None:
    """Lowercase 'drop table' in the response still trips 'DROP TABLE'."""
    case = SecurityEvalCase(
        id="T-003",
        category="bypass",
        description="case-insensitive forbidden",
        setup={},
        input="x",
        expected=SecurityEvalExpectation(
            forbidden_in_response=("DROP TABLE",),
        ),
    )
    ev = SecurityEvaluator()
    r = ev.evaluate(
        case, blocked=True, audit_count=1, response_text="i tried drop table x"
    )
    assert r.passed is False
    assert any("DROP TABLE" in s for s in r.failure_reasons)


def test_evaluator_marks_xfail_when_reason_set() -> None:
    """xfail_reason flips `xfail=True` regardless of passed verdict."""
    case = SecurityEvalCase(
        id="T-004",
        category="permission",
        description="known limitation",
        setup={},
        input="x",
        expected=SecurityEvalExpectation(
            xfail_reason="PLAN-007 has no revocation list",
        ),
    )
    ev = SecurityEvaluator()
    # Deliberately fail every check — xfail flag is still True
    r = ev.evaluate(
        case, blocked=False, audit_count=0, response_text=""
    )
    assert r.xfail is True
    # The case still records why it failed (operator visibility)
    assert r.passed is False


def test_expected_race_outcome_matches_floor_division() -> None:
    """Closed-form mirror of driver math for race cases."""
    case = SecurityEvalCase(
        id="T-RACE",
        category="cost",
        description="race",
        setup={
            "refuse_via": "race",
            "concurrency": 10,
            "budget_usd": 1.0,
            "cost_per_call_usd": 1.0,
        },
        input="x",
    )
    passed, denied = expected_race_outcome(case)
    assert (passed, denied) == (1, 9)


def test_expected_race_outcome_partial_pass() -> None:
    """5 concurrent / budget 2 / cost 1 → 2 pass, 3 deny (PLAN-017 cost-007)."""
    case = SecurityEvalCase(
        id="T-RACE2",
        category="cost",
        description="race partial",
        setup={
            "refuse_via": "race",
            "concurrency": 5,
            "budget_usd": 2.0,
            "cost_per_call_usd": 1.0,
        },
        input="x",
    )
    assert expected_race_outcome(case) == (2, 3)


def test_expected_race_outcome_rejects_zero_cost() -> None:
    """Defensive: cost_per_call_usd=0 is a YAML authoring bug."""
    case = SecurityEvalCase(
        id="T-RACE3",
        category="cost",
        description="bad",
        setup={
            "refuse_via": "race",
            "concurrency": 5,
            "budget_usd": 1.0,
            "cost_per_call_usd": 0.0,
        },
        input="x",
    )
    with pytest.raises(ValueError, match="cost_per_call_usd"):
        expected_race_outcome(case)
