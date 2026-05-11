"""Pydantic Evals data models for Pyrene SQL analyst.

PLAN-005 §1. Every model here is a frozen `StrictBaseModel` with tuples instead
of lists, matching the project-wide invariant (BRIEF §6.1-1). The two top-level
shapes are:

- `EvalCase` — one row of a YAML dataset. Carries the natural-language
  question plus the expected signals the judge will check against the
  agent's `AnalystResponse`.
- `EvalResult` — the judge's verdict per case, including the actual response
  for failure debugging.

The `category` literal narrows the four datasets we ship (PLAN-005 §2):
accuracy / performance / safety / edge. Keeping the discriminator at the case
level (rather than the dataset level) means cross-dataset filtering ("show
me all safety cases regardless of which file") is one-line.
"""

from __future__ import annotations

from typing import Literal

from pyrene_core import Confidence, StrictBaseModel
from pyrene_sql.agent import AnalystResponse

EvalCategory = Literal["accuracy", "performance", "safety", "edge"]


class EvalCase(StrictBaseModel):
    """One natural-language question with judge-verifiable expectations.

    Field rationale:
      - `expected_sql_keywords`: case-insensitive substrings the rendered SQL
        MUST contain (e.g. `("SELECT", "FROM", "category")` for an accuracy
        case). `None` means "do not check SQL keywords" — used by safety
        cases where the only signal is `expected_refusal=True`.
      - `expected_confidence`: pinned label from PRD-001 L-03 ruleset.
        `None` means "any confidence accepted" (rare; typically only edge
        cases with deliberate ambiguity).
      - `expected_refusal`: True when the case expects `refusal != None`.
        Stays separate from `expected_confidence` because a refusal can be
        either high (read-only refusal — PRD-001 §2.2 F1) or low (3-attempt
        exhaustion — PRD-003 §2.1 S3).
      - `expected_row_count`: exact-match row count when known. `None` skips
        the check (most accuracy cases use it; performance/safety leave it
        unset).
    """

    id: str
    question: str
    expected_sql_keywords: tuple[str, ...] | None = None
    expected_confidence: Confidence | None = None
    expected_refusal: bool = False
    expected_row_count: int | None = None
    category: EvalCategory


class EvalResult(StrictBaseModel):
    """Judge verdict for one `EvalCase`.

    `score` is in `[0.0, 1.0]` even though most checks are boolean — this
    leaves the door open for partial credit in `LlmJudge` without changing
    the schema. `passed` is a hard boolean derived from a per-judge threshold
    (KeywordJudge: score == 1.0; LlmJudge: score >= 0.6).

    `actual_response` is embedded by value (not reference) so failed-case
    YAML diffs surface in the baseline review without a second lookup.

    `judge_reasoning` is `None` for KeywordJudge (the deterministic checks
    are self-evident from the case definition) and a short paragraph for
    LlmJudge.
    """

    case_id: str
    passed: bool
    score: float
    actual_response: AnalystResponse
    judge_reasoning: str | None = None


__all__ = [
    "EvalCase",
    "EvalCategory",
    "EvalResult",
]
