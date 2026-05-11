"""Unit tests for `EvalRunner`. PLAN-005 §5.

Verifies the mock_mode integration flow: dataset YAML → runner → judge →
results, with no real agent or LLM call. The integration test
(`test_evals_datasets.py`) covers the same path against the on-disk
recordings; this module focuses on the runner's plumbing (override hooks,
error paths, sentinel Deps handling).
"""

from __future__ import annotations

import pytest

from pyrene_core import Confidence
from pyrene_sql.agent import AnalystResponse
from pyrene_sql.deps import Deps
from pyrene_sql.evals.judge import KeywordJudge
from pyrene_sql.evals.models import EvalCase, EvalResult
from pyrene_sql.evals.runner import EvalRunner

pytestmark = pytest.mark.asyncio


def _stub_response(*, refusal: str | None = None) -> AnalystResponse:
    return AnalystResponse(
        sql="SELECT 1" if refusal is None else None,
        rows=[{"x": 1}] if refusal is None else None,
        row_count=1 if refusal is None else None,
        truncated=False,
        analysis="...",
        confidence=Confidence.high,
        refusal=refusal,
    )


async def test_runner_with_injected_agent_runner_and_judge_skips_io() -> None:
    """Both overrides supplied: no YAML or recordings touched for the agent.

    Note: `run_dataset` still loads the YAML for the case list; the override
    only replaces the agent runner. We verify that the runner walks every
    case and forwards each response to the judge.
    """
    seen_questions: list[str] = []

    async def fake_agent(question: str, _deps: Deps) -> AnalystResponse:
        seen_questions.append(question)
        return _stub_response()

    runner = EvalRunner(agent_runner=fake_agent, judge=KeywordJudge())
    results = await runner.run_dataset("A", mock_mode=True)

    assert len(results) == 20  # dataset A has 20 cases
    assert len(seen_questions) == 20
    # Every result is an EvalResult (not raw AnalystResponse)
    assert all(isinstance(r, EvalResult) for r in results)


async def test_runner_invokes_real_judge_on_each_case() -> None:
    """Custom judge is called once per case with `(case, response)`."""
    calls: list[tuple[EvalCase, AnalystResponse]] = []

    class RecordingJudge:
        async def evaluate(
            self, case: EvalCase, response: AnalystResponse
        ) -> EvalResult:
            calls.append((case, response))
            return EvalResult(
                case_id=case.id,
                passed=True,
                score=1.0,
                actual_response=response,
            )

    async def fake_agent(_question: str, _deps: Deps) -> AnalystResponse:
        return _stub_response()

    runner = EvalRunner(agent_runner=fake_agent, judge=RecordingJudge())
    results = await runner.run_dataset("B", mock_mode=True)

    assert len(results) == 10
    assert len(calls) == 10
    # Cases come out of the YAML in declared order; first one is B-001.
    assert calls[0][0].id == "B-001"


async def test_runner_mock_mode_loads_recordings_when_no_override() -> None:
    """No agent_runner override → runner reads recordings from disk."""
    runner = EvalRunner(judge=KeywordJudge())
    results = await runner.run_dataset("A", mock_mode=True)
    # We don't assert ALL pass here (that's the integration test's job) —
    # we only assert the recordings were found and the runner produced
    # one result per case.
    assert len(results) == 20
    assert all(isinstance(r, EvalResult) for r in results)


async def test_runner_real_mode_without_deps_raises() -> None:
    """`mock_mode=False` requires either a Deps or an agent_runner override."""
    runner = EvalRunner(judge=KeywordJudge())
    with pytest.raises(ValueError, match="mock_mode=False"):
        await runner.run_dataset("A", mock_mode=False, deps=None)


async def test_runner_unknown_dataset_raises() -> None:
    runner = EvalRunner(judge=KeywordJudge())
    with pytest.raises(KeyError):
        await runner.run_dataset("Z", mock_mode=True)


async def test_runner_missing_recording_for_case_raises() -> None:
    """If a case has no recorded response in mock_mode, the lookup raises.

    We simulate this by injecting an agent_runner that itself raises
    KeyError — the runner does not swallow it (failures must surface, not
    silently downgrade the score).
    """

    async def missing_runner(question: str, _deps: Deps) -> AnalystResponse:
        raise KeyError(f"No recording for {question!r}")

    runner = EvalRunner(agent_runner=missing_runner, judge=KeywordJudge())
    with pytest.raises(KeyError):
        await runner.run_dataset("A", mock_mode=True)
