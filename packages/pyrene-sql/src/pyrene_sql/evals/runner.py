"""Eval runner: dataset → agent (or mock) → judge → results.

PLAN-005 §1. Two execution modes:

- `mock_mode=True` (CI default, ADR-012 evals-fast). Looks up each case's
  pre-recorded `AnalystResponse` from `tests/evals/recordings/`. No LLM
  calls, cost = $0, deterministic. The "agent" is a dict lookup; the judge
  remains real.
- `mock_mode=False` (nightly evals-full). Calls `run_with_retry` against
  the real agent. Requires `LIVE_TESTS=1` and a provider key. Caller is
  responsible for setting up `Deps` (DB session + retriever).

Why the runner takes the dataset *name* rather than the loaded cases:
keeping the file-system entry point inside the runner means CLI / pytest /
nightly cron all share one execution path. The runner also picks the judge
based on category — datasets A/B/C use `KeywordJudge`, dataset D uses
`LlmJudge` (PLAN-005 §1, "LlmJudge 사용은 dataset D만").
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from pyrene_sql.agent import AnalystResponse
from pyrene_sql.deps import Deps
from pyrene_sql.evals.judge import JudgeProtocol, KeywordJudge, LlmJudge
from pyrene_sql.evals.loader import load_dataset
from pyrene_sql.evals.models import EvalResult
from pyrene_sql.evals.recordings import load_recordings

# Type alias: caller-supplied callable that maps a question + Deps to an
# AnalystResponse. We hide `run_with_retry` behind this so unit tests can
# inject a deterministic stub without monkeypatching the import. Default
# value (set inside `EvalRunner.__init__`) is `agent.run_with_retry`.
AgentRunner = Callable[[str, Deps], Awaitable[AnalystResponse]]


@dataclass(frozen=True)
class _MockAgentRunner:
    """Internal runner that returns pre-recorded responses.

    Bound to a single dataset. `case_id` is derived from `question` via the
    `lookup` map built in `EvalRunner.run_dataset`. Frozen + slots-style
    via dataclass(frozen=True) so it can be safely shared across asyncio
    tasks if we add concurrency later.
    """

    lookup: dict[str, AnalystResponse]  # question → recorded response

    async def __call__(self, question: str, _deps: Deps) -> AnalystResponse:
        try:
            return self.lookup[question]
        except KeyError as exc:
            raise KeyError(
                f"No recording for question {question!r}. Either add it to "
                f"the dataset's recordings JSON or run with mock_mode=False."
            ) from exc


def _judge_for_dataset(name: str) -> JudgeProtocol:
    """Map a dataset short-name to its judge.

    Dataset D (edge cases) is the only one that uses `LlmJudge` because the
    correctness signal there is "did the analysis text address the user's
    ambiguity?" — a fuzzy property KeywordJudge cannot grade. A/B/C all
    have crisp keyword/confidence/refusal signals.
    """
    if name == "D":
        return LlmJudge()
    return KeywordJudge()


class EvalRunner:
    """Compose dataset → agent → judge → results in one async pass.

    Stateless aside from the optional injected `agent_runner` and `judge`
    overrides used by unit tests. The runner accepts both because each is
    independently substitutable: mock the agent but keep the real judge,
    or vice versa.
    """

    def __init__(
        self,
        *,
        agent_runner: AgentRunner | None = None,
        judge: JudgeProtocol | None = None,
    ) -> None:
        self._agent_runner_override = agent_runner
        self._judge_override = judge

    async def run_dataset(
        self,
        name: str,
        *,
        mock_mode: bool = True,
        deps: Deps | None = None,
    ) -> tuple[EvalResult, ...]:
        """Run every case in dataset `name` and return judge results.

        Parameters:
          - `name`: one of "A"/"B"/"C"/"D" (matches `loader._DATASET_FILES`).
          - `mock_mode`: True (default) replays pre-recorded responses;
            False calls `run_with_retry`. The latter requires `deps`.
          - `deps`: only used when `mock_mode=False`. Mocked path tolerates
            `None` — the recorded response is used as-is.

        Errors:
          - `FileNotFoundError`: dataset YAML or recordings JSON missing.
          - `KeyError`: a case has no recording (mock mode only).
          - `ValueError`: when `mock_mode=False` and `deps is None`.
        """
        cases = load_dataset(name)
        judge = self._judge_override or _judge_for_dataset(name)
        agent_runner = self._agent_runner_override or self._build_agent_runner(
            name, mock_mode
        )

        if not mock_mode and deps is None and self._agent_runner_override is None:
            raise ValueError(
                "mock_mode=False requires a `deps` argument so the real "
                "agent has a DB session to query."
            )

        results: list[EvalResult] = []
        for case in cases:
            # In mock_mode the runner never dereferences `deps`, so passing
            # `None` is safe. We narrow with cast() rather than fabricating
            # a fake Deps because Deps is a frozen dataclass with a required
            # AsyncSession field — mocking that here would obscure the fact
            # that the mock runner is the only thing keeping it un-touched.
            response = await agent_runner(
                case.question,
                deps if deps is not None else _MOCK_SENTINEL_DEPS,
            )
            result = await judge.evaluate(case, response)
            results.append(result)
        return tuple(results)

    def _build_agent_runner(self, name: str, mock_mode: bool) -> AgentRunner:
        """Pick the default runner for the requested mode.

        Pulled out so the constructor stays trivial — this method may be
        invoked once per `run_dataset` call when no override is set.
        """
        if mock_mode:
            recordings = load_recordings(name)
            cases = load_dataset(name)
            # Keying by question (not case_id) lets the agent runner stay
            # IO-symmetric: it sees only `(question, deps)` like the real
            # `run_with_retry`. Duplicate questions across a dataset would
            # collapse here — acceptable because `EvalCase.id` is unique
            # but `question` SHOULD be too (loader does not enforce, but
            # baselines surface duplicates as a single result).
            lookup: dict[str, AnalystResponse] = {}
            for case in cases:
                if case.id in recordings:
                    lookup[case.question] = recordings[case.id]
            return _MockAgentRunner(lookup=lookup)

        # Defer the import: in mock_mode (CI default) we avoid pulling in
        # pyrene_sql.agent's heavier imports at module-load time. Tests
        # that exercise mock_mode never trigger this branch.
        from pyrene_sql.agent import run_with_retry as real_runner

        async def _real(question: str, deps: Deps) -> AnalystResponse:
            return await real_runner(question, deps)

        return _real


# Module-level sentinel used when `mock_mode=True` and the caller passes no
# Deps. We typecast a plain object() through `cast(Deps, ...)` — the mock
# agent runner never dereferences this value, so the lie is contained.
_MOCK_SENTINEL_DEPS: Deps = cast(Deps, object())


async def run_dataset(
    name: str,
    *,
    mock_mode: bool = True,
    deps: Deps | None = None,
) -> tuple[EvalResult, ...]:
    """Module-level convenience wrapper around `EvalRunner.run_dataset`.

    Mirrors the `agent.run_with_retry` ergonomic — one import, one call.
    Tests that need to inject a custom judge/runner instantiate `EvalRunner`
    directly.
    """
    return await EvalRunner().run_dataset(name, mock_mode=mock_mode, deps=deps)


__all__ = ["AgentRunner", "EvalRunner", "run_dataset"]
