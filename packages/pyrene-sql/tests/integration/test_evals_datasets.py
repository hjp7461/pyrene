"""Integration test: 4 datasets load + run (mock_mode=True) + baseline regression.

PLAN-005 §5. This test does not need testcontainers (it is mocked end-to-
end) but lives in the integration tree because:

1. It exercises file IO (YAML loading + JSON recordings + JSON baselines)
   which is closer in spirit to integration than unit testing.
2. It is the canonical "evals-fast" stage from ADR-012 — CI's PR gate.
3. It gates baseline drift, which is the critical signal we run on every
   PR (without it, regression detection is gone).

The four assertions per dataset are:
  - cases load without validation error
  - run_dataset(mock_mode=True) yields one result per case
  - every result's `score` matches the baseline within float tolerance
  - every result's `passed` matches the baseline exactly
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import pytest

from pyrene_sql.evals import KeywordJudge, load_dataset
from pyrene_sql.evals.runner import EvalRunner

# Mock_mode evals do NOT touch the DB or any docker container, but they DO
# cross module boundaries (loader + recordings + runner + judge) — which
# matches the spirit of an "integration" test. We declare both markers so
# CI selectors can target either:
#   - `pytest -m integration` runs this alongside the testcontainers tests
#   - `pytest -m "not integration"` skips it (it would still run with no
#     selector, since pytestmark = list-of-marks is additive).
# ADR-012 evals-fast invokes this file directly via path, not by marker.
pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


_BASELINES_DIR = (
    Path(__file__).resolve().parents[1] / "baselines" / "evals"
)
_DATASETS = ("A", "B", "C", "D")
_SCORE_TOLERANCE = 1e-6


class _BaselineEntry(TypedDict):
    expected_score: float
    expected_pass: bool


def _load_baseline(name: str) -> dict[str, _BaselineEntry]:
    path = _BASELINES_DIR / f"dataset_{name.lower()}.json"
    with path.open("r", encoding="utf-8") as fh:
        loaded: dict[str, _BaselineEntry] = json.load(fh)
    return loaded


@pytest.mark.parametrize("dataset_name", _DATASETS)
async def test_dataset_loads_cases(dataset_name: str) -> None:
    """YAML parses without validation error and produces at least one case."""
    cases = load_dataset(dataset_name)
    assert len(cases) > 0
    # Every case must have a unique id within its dataset.
    ids = [c.id for c in cases]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("dataset_name", _DATASETS)
async def test_dataset_runs_in_mock_mode(dataset_name: str) -> None:
    """run_dataset returns one EvalResult per case with score in [0, 1]."""
    # Dataset D normally uses LlmJudge; for mock_mode CI we override to
    # KeywordJudge so the test is hermetic (no OpenAI key required).
    runner = EvalRunner(judge=KeywordJudge())
    results = await runner.run_dataset(dataset_name, mock_mode=True)

    cases = load_dataset(dataset_name)
    assert len(results) == len(cases)
    for r in results:
        assert 0.0 <= r.score <= 1.0


@pytest.mark.parametrize("dataset_name", _DATASETS)
async def test_dataset_baseline_regression(dataset_name: str) -> None:
    """Per-case score + pass match the committed baseline.

    This is the regression gate: any prompt / dataset / recording change
    that shifts a single case's score also shifts this assertion. The
    fix-forward path is "regenerate baselines + send a PR with the
    `baseline-override` label" (ADR-012 §4).
    """
    baseline = _load_baseline(dataset_name)
    runner = EvalRunner(judge=KeywordJudge())
    results = await runner.run_dataset(dataset_name, mock_mode=True)

    # Baseline must cover every case in the dataset (no orphaned IDs).
    actual_ids = {r.case_id for r in results}
    baseline_ids = set(baseline.keys())
    assert actual_ids == baseline_ids, (
        f"Dataset {dataset_name}: case IDs mismatch baseline. "
        f"In dataset only: {actual_ids - baseline_ids}; "
        f"in baseline only: {baseline_ids - actual_ids}."
    )

    for r in results:
        expected = baseline[r.case_id]
        assert r.passed == expected["expected_pass"], (
            f"{r.case_id}: passed={r.passed} but baseline expects "
            f"{expected['expected_pass']}"
        )
        expected_score = expected["expected_score"]
        assert abs(r.score - expected_score) <= _SCORE_TOLERANCE, (
            f"{r.case_id}: score={r.score} but baseline expects "
            f"{expected_score}"
        )


async def test_all_four_datasets_pass_baseline_in_aggregate() -> None:
    """Top-level smoke: every dataset hits 100% of its baseline.

    This is a redundant check (the per-dataset regression test above is
    stricter) but it gives one obvious red/green line in the CI summary,
    which is what reviewers look at first.
    """
    runner = EvalRunner(judge=KeywordJudge())
    total = 0
    for name in _DATASETS:
        results = await runner.run_dataset(name, mock_mode=True)
        baseline = _load_baseline(name)
        for r in results:
            assert r.passed == baseline[r.case_id]["expected_pass"]
            total += 1
    assert total == 50  # 20 + 10 + 10 + 10
