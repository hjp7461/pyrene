"""Per-case baseline IO for security evals.

PLAN-017 §Day 1 §작업 순서 6. Each case has its own JSON file in
`tests/baselines/security/<category>/<case_id>.json` rather than one
mega-file per category — the rationale (ADR-012 §4):

  - Per-file granularity makes baseline diffs in PRs trivially readable
    (one changed case = one changed file).
  - CODEOWNERS rules can match `tests/baselines/security/**` and require
    Sr. QA review on any baseline drift.
  - When a new case is added, the only file changed in the baseline
    directory is the new file — no merge conflicts on shared JSON.

Baseline schema:

```json
{
  "case_id": "BYP-001",
  "expected_passed": true,
  "expected_blocked": true,
  "expected_audit_count": 1,
  "response_text_sha256": "deadbeef..."
}
```

We pin a SHA-256 of `response_text` rather than the raw text because:
  - Refusal messages can be long (200+ chars); the hash compresses.
  - A hash mismatch is the regression signal — the diff between
    expected and actual is recoverable from the failing test's log.
  - Avoids accidentally checking machine-readable error fields into the
    baseline (only the hash, plus the deterministic boolean signals).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict

from pyrene_sql.evals.security.models import (
    SecurityCategory,
    SecurityEvalResult,
)

_BASELINES_DIR: Path = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "baselines"
    / "security"
)


class BaselineEntry(TypedDict):
    """JSON shape of a per-case baseline file.

    `case_id` is redundant with the filename but kept inline so the
    file is self-describing in greps. `response_text_sha256` is the
    hex digest (lowercase) of the response text encoded as UTF-8.
    """

    case_id: str
    expected_passed: bool
    expected_blocked: bool
    expected_audit_count: int
    response_text_sha256: str


def baseline_path(category: SecurityCategory, case_id: str) -> Path:
    """Resolve the JSON file for a given (category, case_id)."""
    return _BASELINES_DIR / category / f"{case_id}.json"


def load_baseline(
    category: SecurityCategory, case_id: str
) -> BaselineEntry:
    """Load the baseline for one case.

    Raises:
      - `FileNotFoundError`: baseline file missing — the case is
        unaccounted for in CI, which the integration test surfaces as
        a hard fail rather than silently passing.
    """
    path = baseline_path(category, case_id)
    if not path.exists():
        raise FileNotFoundError(
            f"No baseline for case {case_id!r} (category {category!r}) "
            f"at {path}."
        )
    with path.open("r", encoding="utf-8") as fh:
        loaded: BaselineEntry = json.load(fh)
    return loaded


def write_baseline(
    category: SecurityCategory,
    result: SecurityEvalResult,
) -> Path:
    """Write or overwrite the baseline file for a result.

    Writers are gated behind `baseline-override` PR labels at the CI
    level (ADR-012 §4) — this function does not police that itself.
    It is invoked from a maintainer-run regenerator (not by the
    PR-gate `security-evals` job).
    """
    path = baseline_path(category, result.case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: BaselineEntry = {
        "case_id": result.case_id,
        "expected_passed": result.passed,
        "expected_blocked": result.blocked,
        "expected_audit_count": result.audit_count,
        "response_text_sha256": _hash_text(result.response_text),
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(entry, fh, indent=2, ensure_ascii=False)
        fh.write("\n")  # POSIX-friendly trailing newline
    return path


def _hash_text(text: str) -> str:
    """SHA-256 hex digest of a string. Stable across platforms."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assert_matches_baseline(
    category: SecurityCategory,
    result: SecurityEvalResult,
) -> tuple[bool, tuple[str, ...]]:
    """Compare a fresh result to its baseline.

    Returns `(matched, reasons)`. `reasons` is non-empty when at least
    one field disagrees; each entry is human-readable.

    Used by the integration test in lieu of bare `assert`s — gathering
    every reason in one shot makes baseline-drift PRs easier to triage
    (one log line per drift, not "stop at the first failed assert").
    """
    baseline = load_baseline(category, result.case_id)
    reasons: list[str] = []

    if result.passed != baseline["expected_passed"]:
        reasons.append(
            f"passed={result.passed} but baseline expects "
            f"{baseline['expected_passed']}"
        )
    if result.blocked != baseline["expected_blocked"]:
        reasons.append(
            f"blocked={result.blocked} but baseline expects "
            f"{baseline['expected_blocked']}"
        )
    if result.audit_count != baseline["expected_audit_count"]:
        reasons.append(
            f"audit_count={result.audit_count} but baseline expects "
            f"{baseline['expected_audit_count']}"
        )
    actual_hash = _hash_text(result.response_text)
    if actual_hash != baseline["response_text_sha256"]:
        reasons.append(
            f"response_text sha256={actual_hash[:12]}... but baseline "
            f"expects {baseline['response_text_sha256'][:12]}..."
        )
    return (not reasons, tuple(reasons))


__all__ = [
    "BaselineEntry",
    "assert_matches_baseline",
    "baseline_path",
    "load_baseline",
    "write_baseline",
]
