"""YAML dataset loader for the eval harness.

PLAN-005 §2 names four datasets that ship as YAML alongside the test tree.
This loader resolves a dataset name to its on-disk path, parses YAML, and
validates each entry through `EvalCase`. The shape:

```yaml
cases:
  - id: A-001
    question: "..."
    category: accuracy
    expected_sql_keywords: [SELECT, FROM, category]
    expected_confidence: high
    expected_refusal: false
    expected_row_count: 16
```

`expected_confidence` is a string in YAML; Pydantic coerces it to the
`Confidence` enum on validation. We do not allow extra fields (StrictBaseModel
forbids it), so a typo in a key fails fast in CI rather than silently
ignoring half the case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pyrene_sql.evals.models import EvalCase

# Datasets live next to the tests rather than inside `src/` because they are
# test fixtures, not runtime artifacts. Resolving via __file__ keeps the
# runner agnostic to install location (works under `uv run` and editable
# installs alike). parents[3] points at the package root
# (`packages/pyrene-sql/`); from there we hop into `tests/evals/datasets/`.
_DATASETS_DIR: Path = (
    Path(__file__).resolve().parents[3] / "tests" / "evals" / "datasets"
)

_DATASET_FILES: dict[str, str] = {
    "A": "dataset_a_accuracy.yaml",
    "B": "dataset_b_performance.yaml",
    "C": "dataset_c_safety.yaml",
    "D": "dataset_d_edge.yaml",
}


def dataset_path(name: str) -> Path:
    """Resolve a dataset short-name (A/B/C/D) to its YAML file path.

    Raises `KeyError` (caller's responsibility to translate to user error)
    when the name is unknown — we deliberately keep the four-letter alphabet
    closed so a typo fails fast.
    """
    return _DATASETS_DIR / _DATASET_FILES[name]


def load_dataset(name: str) -> tuple[EvalCase, ...]:
    """Load a YAML dataset and validate every case.

    Returns a tuple (frozen ordering) so downstream code cannot accidentally
    mutate the list and confuse a parallel test. The YAML root must be
    `{"cases": [...]}`; anything else raises a clear error.
    """
    path = dataset_path(name)
    if not path.exists():
        raise FileNotFoundError(
            f"Dataset {name!r} not found at {path}. Expected one of "
            f"{sorted(_DATASET_FILES)}."
        )

    with path.open("r", encoding="utf-8") as fh:
        raw: Any = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "cases" not in raw:
        raise ValueError(
            f"Dataset {name!r} ({path}) must have a top-level 'cases' key."
        )
    cases_list = raw["cases"]
    if not isinstance(cases_list, list):
        raise ValueError(
            f"Dataset {name!r}: 'cases' must be a YAML list, got "
            f"{type(cases_list).__name__}."
        )

    return tuple(EvalCase.model_validate(item) for item in cases_list)


__all__ = ["dataset_path", "load_dataset"]
