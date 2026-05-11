"""Integration test: 42 security eval cases pass + match baselines.

PLAN-017 Wave 9 §Day 3 — the CI-gating regression net. This test runs
every case from `bypass.yaml` / `cost.yaml` / `permission.yaml` through
the mock driver + evaluator and compares the verdict against the per-
case JSON baseline.

Why this lives in `tests/integration/` (with the `_security` suffix):
  - It crosses module boundaries (loader + driver + evaluator + baseline
    IO) which matches the spirit of an integration test.
  - It is the canonical `security-evals` CI stage (ADR-012 stage 6).
  - No DB / docker is touched, but the test counts >40 cases and runs
    in a few seconds — fast enough for PR gating.

What this DOES NOT test (out of scope for the mocked CI stage):
  - The real RBAC permission resolver (PLAN-010/011) — has its own
    integration tests under `tests/integration/test_*_rbac*.py`.
  - The real budget hook / advisory lock (PLAN-014) — same.
  - The real WORM audit trigger (PLAN-015) — same.

The mocked driver verifies the **harness**; the underlying gates are
verified in their own packages. PRD-017 §6 is about both: this test
closes the eval-suite gap.
"""

from __future__ import annotations

import pytest

from pyrene_sql.evals.security import (
    MockSecurityDriver,
    SecurityCategory,
    SecurityEvaluator,
    assert_matches_baseline,
    expected_race_outcome,
    load_all_security_datasets,
    load_security_dataset,
)

# All assertions are async (driver.run is async). `auto` mode handles
# the marker globally but we declare it for IDEs.
pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


_CATEGORIES: tuple[SecurityCategory, ...] = ("bypass", "cost", "permission")


@pytest.mark.parametrize("category", _CATEGORIES)
async def test_security_dataset_runs_clean(
    category: SecurityCategory,
) -> None:
    """Every case in a category passes (or is xfail) — no surprise fails."""
    driver = MockSecurityDriver()
    evaluator = SecurityEvaluator()
    fails: list[str] = []
    for case in load_security_dataset(category):
        outcome = await driver.run(case)
        result = evaluator.evaluate(
            case,
            blocked=outcome.blocked,
            audit_count=outcome.audit_count,
            response_text=outcome.response_text,
        )
        if not result.passed and not result.xfail:
            fails.append(
                f"{result.case_id}: {', '.join(result.failure_reasons)}"
            )
    assert not fails, (
        f"Category {category!r} has unexpected failures:\n"
        + "\n".join(fails)
    )


@pytest.mark.parametrize("category", _CATEGORIES)
async def test_security_dataset_matches_baselines(
    category: SecurityCategory,
) -> None:
    """Per-case baseline regression check.

    Any drift in `passed` / `blocked` / `audit_count` /
    `response_text_sha256` triggers a fail. The fix-forward path
    (ADR-012 §4) is "regenerate baselines + send a PR with the
    `baseline-override` label" — never silently regenerate.
    """
    driver = MockSecurityDriver()
    evaluator = SecurityEvaluator()
    drift: list[str] = []
    for case in load_security_dataset(category):
        outcome = await driver.run(case)
        result = evaluator.evaluate(
            case,
            blocked=outcome.blocked,
            audit_count=outcome.audit_count,
            response_text=outcome.response_text,
        )
        matched, reasons = assert_matches_baseline(category, result)
        if not matched:
            drift.append(
                f"{result.case_id}: {' | '.join(reasons)}"
            )
    assert not drift, (
        f"Baseline drift in {category!r}:\n" + "\n".join(drift)
    )


async def test_all_42_cases_run_through_pipeline() -> None:
    """Top-line counter — 42 cases load + run + score.

    Mirror of PLAN-017 Day 3 §완료 기준 ("42+ cases all pass"). One
    obvious red/green line in the CI summary.
    """
    cases = load_all_security_datasets()
    assert len(cases) == 42

    driver = MockSecurityDriver()
    evaluator = SecurityEvaluator()
    passed = 0
    xfail = 0
    for case in cases:
        outcome = await driver.run(case)
        result = evaluator.evaluate(
            case,
            blocked=outcome.blocked,
            audit_count=outcome.audit_count,
            response_text=outcome.response_text,
        )
        if result.xfail:
            xfail += 1
        elif result.passed:
            passed += 1

    # 41 passing + 1 xfail = 42 accounted for. PLAN-017 Day 3 §완료 기준
    # specifies "12개 (xfail 1개 제외 시 11개) 모두 통과".
    assert passed == 41
    assert xfail == 1


async def test_cost_006_race_yields_exact_1_pass_9_deny() -> None:
    """PLAN-017 §Day 2 §완료 기준 — exact denial count, not 'at least 1'.

    Pin the headline race case at the top level so a regression in
    `_run_race` (e.g. losing the lock, dropping the barrier) shows up
    in CI with a dedicated red line — easier to triage than a generic
    'baseline drift' failure.
    """
    cases = {c.id: c for c in load_security_dataset("cost")}
    case = cases["COST-006"]
    passed, denied = expected_race_outcome(case)
    assert (passed, denied) == (1, 9)

    outcome = await MockSecurityDriver().run(case)
    # Driver emits audit row per call regardless of pass/deny —
    # must_audit_count is the total fan-out.
    assert outcome.audit_count == case.expected.must_audit_count == 10


async def test_perm_010_jwt_revocation_remains_xfail() -> None:
    """Known-limitation tripwire — fires when PLAN-007 adds a denylist.

    When this test's `result.passed` flips to True, the operator should
    remove the `refuse_via: xfail` from PERM-010 (and the
    `xfail_reason`) and re-baseline. Until then the test passes as
    long as the xfail flag is set.
    """
    case = next(
        c for c in load_security_dataset("permission") if c.id == "PERM-010"
    )
    assert case.expected.xfail_reason is not None

    outcome = await MockSecurityDriver().run(case)
    result = SecurityEvaluator().evaluate(
        case,
        blocked=outcome.blocked,
        audit_count=outcome.audit_count,
        response_text=outcome.response_text,
    )
    assert result.xfail is True
    # The current implementation fails the case (deliberately). When
    # this assertion starts failing, congratulations — PLAN-007
    # implemented JWT revocation.
    assert result.passed is False, (
        "PERM-010 unexpectedly passed — JWT revocation is implemented? "
        "Remove `refuse_via: xfail` from permission.yaml."
    )


async def test_every_case_has_baseline_file() -> None:
    """No orphan cases — every YAML entry has a JSON baseline neighbor.

    Catches the "added a case but forgot to regenerate baselines" bug
    that would otherwise surface only on the per-case parametrize.
    """
    from pyrene_sql.evals.security import baseline_path

    missing: list[str] = []
    for case in load_all_security_datasets():
        path = baseline_path(case.category, case.id)
        if not path.exists():
            missing.append(f"{case.category}/{case.id} → {path}")
    assert not missing, (
        "Cases without baseline files:\n" + "\n".join(missing)
    )
