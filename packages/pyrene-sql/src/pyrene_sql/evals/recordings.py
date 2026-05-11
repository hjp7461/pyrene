"""Pre-recorded `AnalystResponse` fixtures for `mock_mode` evals.

PLAN-005 §1 (`runner.py`). The CI default is mocked: instead of calling
`run_with_retry` we look up a recorded `AnalystResponse` keyed by
`(dataset_name, case_id)` and return it as if the agent had emitted it.

Why this pattern instead of `pytest-vcr` or `pydantic-ai`'s `FunctionModel`:

- VCR records HTTP traffic; we want to record the *agent's terminal output*
  including `attempts`, `confidence`, `analysis`. Recording at the response
  layer is cheaper to maintain and a 1:1 mirror of `AnalystResponse`.
- `FunctionModel` requires scripting the model conversation per-case, which
  is acceptable in unit tests but burdensome at 50+ eval cases.

Recording format on disk: `tests/evals/recordings/<dataset>.json`, a JSON
object mapping `case_id` → `AnalystResponse.model_dump(mode="json")`. We
store the dump in JSON (not YAML) because Pydantic's validation round-trip
is canonical for JSON, eliminating "did the YAML loader coerce a stringy
`row_count`?" debugging.

Refresh discipline (ADR-012): when the analyst's prompt or model changes,
nightly evals-full re-runs and a maintainer regenerates the recordings via
the `pyrene-sql evals refresh-recordings` CLI (out-of-scope for this PLAN
but reserved as a follow-up — PLAN-005 §note).
"""

from __future__ import annotations

import json
from pathlib import Path

from pyrene_sql.agent import AnalystResponse

_RECORDINGS_DIR: Path = (
    Path(__file__).resolve().parents[3] / "tests" / "evals" / "recordings"
)


def recordings_path(dataset_name: str) -> Path:
    """Return the on-disk JSON path for a dataset's recordings."""
    return _RECORDINGS_DIR / f"{dataset_name.lower()}.json"


def load_recordings(dataset_name: str) -> dict[str, AnalystResponse]:
    """Load the recordings JSON for a dataset and validate each entry.

    Returns a dict so callers can do `recordings[case_id]` directly. We
    validate at load time (not lookup time) so a malformed recording fails
    fast in `evals-fast` CI rather than mid-run.
    """
    path = recordings_path(dataset_name)
    if not path.exists():
        raise FileNotFoundError(
            f"No recordings found for dataset {dataset_name!r} at {path}. "
            "Run `pyrene-sql evals refresh-recordings` (or hand-author the "
            "JSON) before running mock_mode."
        )

    with path.open("r", encoding="utf-8") as fh:
        raw: dict[str, object] = json.load(fh)

    return {
        case_id: AnalystResponse.model_validate(payload)
        for case_id, payload in raw.items()
    }


__all__ = ["load_recordings", "recordings_path"]
