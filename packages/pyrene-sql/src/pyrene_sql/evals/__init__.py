"""Pyrene evals — Pydantic-style dataset harness for the SQL analyst.

PLAN-005 §1. Public surface:

- `EvalCase`, `EvalResult`, `EvalCategory` — frozen models for cases + judge
  verdicts (PLAN-005 §1).
- `JudgeProtocol`, `KeywordJudge`, `LlmJudge` — judges (PLAN-005 §1).
- `EvalRunner`, `run_dataset` — orchestration entry points.
- `load_dataset`, `dataset_path`, `load_recordings`, `recordings_path` —
  YAML/JSON IO helpers re-exported for tooling (CLI, baseline updater).

Phase boundary: this module never reaches into `pyrene-core/observability/`
(PLAN-006). PLAN-017's security evals live in the sibling sub-package
`pyrene_sql.evals.security` so the two evolve independently — importing
this module does NOT pull `security` into scope (the security sub-
package has its own `__init__.py` and is opt-in).
"""

from pyrene_sql.evals.judge import JudgeProtocol, KeywordJudge, LlmJudge
from pyrene_sql.evals.loader import dataset_path, load_dataset
from pyrene_sql.evals.models import EvalCase, EvalCategory, EvalResult
from pyrene_sql.evals.recordings import load_recordings, recordings_path
from pyrene_sql.evals.runner import AgentRunner, EvalRunner, run_dataset

__all__ = [
    "AgentRunner",
    "EvalCase",
    "EvalCategory",
    "EvalResult",
    "EvalRunner",
    "JudgeProtocol",
    "KeywordJudge",
    "LlmJudge",
    "dataset_path",
    "load_dataset",
    "load_recordings",
    "recordings_path",
    "run_dataset",
]
