"""Unit tests for `bin/measure_chunk_recall.py` pure helpers.

The IO-heavy `measure_one_variant` / DB summary helpers are exercised
only by live runs (PRD-043 §"Out of scope" — Docker-dependent). The
helpers below are pure functions and unit-testable.

The script lives outside `packages/` so we add `bin/` to `sys.path` for
this module.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# bin/ is not a package; add it to sys.path so we can import the script.
# parents: 0=unit, 1=tests, 2=pyrene-sql, 3=packages, 4=repo root
_BIN_DIR = Path(__file__).resolve().parents[4] / "bin"
sys.path.insert(0, str(_BIN_DIR))

import measure_chunk_recall as mcr  # noqa: E402

# ---------------------------------------------------------------------------
# load_dataset
# ---------------------------------------------------------------------------


def test_load_dataset_validates_30_cases(tmp_path: Path) -> None:
    """The 30-case invariant matches `test_top_3_accuracy_meets_90_percent_threshold`."""
    fake = tmp_path / "ds.yaml"
    fake.write_text(
        "cases:\n"
        + "\n".join(
            f"  - id: q-{i}\n    query: 'q{i}'\n    expected_tables: [t{i}]"
            for i in range(30)
        ),
        encoding="utf-8",
    )
    cases = mcr.load_dataset(fake)
    assert len(cases) == 30
    assert cases[0]["id"] == "q-0"


def test_load_dataset_rejects_wrong_count(tmp_path: Path) -> None:
    fake = tmp_path / "ds.yaml"
    fake.write_text(
        "cases:\n"
        + "\n".join(
            f"  - id: q-{i}\n    query: 'q{i}'\n    expected_tables: [t]"
            for i in range(10)
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expected 30 cases"):
        mcr.load_dataset(fake)


# ---------------------------------------------------------------------------
# compute_accuracy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hits,total,expected",
    [
        (27, 30, 0.9),
        (30, 30, 1.0),
        (0, 30, 0.0),
        (15, 30, 0.5),
        (1, 0, 0.0),  # guard: total == 0 → 0.0
        (1, -5, 0.0),  # guard: negative → 0.0
    ],
)
def test_compute_accuracy(hits: int, total: int, expected: float) -> None:
    assert mcr.compute_accuracy(hits, total) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------


def test_percentile_empty_returns_zero() -> None:
    assert mcr._percentile([], 50) == 0.0


def test_percentile_single_value_returns_value() -> None:
    assert mcr._percentile([42.0], 95) == 42.0


def test_percentile_p50_is_median() -> None:
    values = list(range(1, 101))  # 1..100
    median = mcr._percentile(values, 50)
    assert 49 <= median <= 51


# ---------------------------------------------------------------------------
# render_markdown_report
# ---------------------------------------------------------------------------


def _make_result(
    *, key: str, label: str, top3: float, misses: int = 0
) -> mcr.VariantResult:
    variant = mcr.Variant(
        key=key,
        label=label,
        description=f"variant {key} desc",
        ef_search=200,
        chunk_type_filter=None,
    )
    return mcr.VariantResult(
        variant=variant,
        top_3_accuracy=top3,
        top_5_accuracy=top3 + 0.05,
        misses=tuple(
            mcr.MissRecord(
                case_id=f"q-{i}",
                query=f"miss-{i}",
                expected=("payment",),
                got_top_3=("film", "actor"),
            )
            for i in range(misses)
        ),
        latency_p50_ms=12.0,
        latency_p95_ms=42.0,
        total_cases=30,
    )


def test_render_markdown_report_includes_all_variants() -> None:
    results = (
        _make_result(key="A", label="Hybrid + ef200", top3=0.97),
        _make_result(key="B", label="Hybrid + ef100", top3=0.97),
        _make_result(key="C", label="Pure + ef200", top3=0.87),
    )
    md = mcr.render_markdown_report(
        git_sha="abc1234",
        timestamp_iso="2026-05-14 08:00:00 UTC",
        embedder_model="text-embedding-3-small",
        embedder_dimensions=1024,
        dataset_path="tests/data/x.yaml",
        chunk_count=111,
        table_count=16,
        column_count=95,
        results=results,
    )
    assert "abc1234" in md
    assert "Hybrid + ef200" in md
    assert "Hybrid + ef100" in md
    assert "Pure + ef200" in md
    assert "97.0%" in md  # top-3
    assert "87.0%" in md
    assert "111 chunks" in md or "111" in md


def test_render_markdown_report_shows_no_misses_marker() -> None:
    results = (_make_result(key="A", label="Hybrid", top3=1.0, misses=0),)
    md = mcr.render_markdown_report(
        git_sha="x",
        timestamp_iso="t",
        embedder_model="m",
        embedder_dimensions=1024,
        dataset_path="p",
        chunk_count=1,
        table_count=1,
        column_count=0,
        results=results,
    )
    assert "no misses" in md


def test_render_markdown_report_lists_misses_with_query_and_expected() -> None:
    results = (_make_result(key="A", label="Hybrid", top3=0.93, misses=2),)
    md = mcr.render_markdown_report(
        git_sha="x",
        timestamp_iso="t",
        embedder_model="m",
        embedder_dimensions=1024,
        dataset_path="p",
        chunk_count=1,
        table_count=1,
        column_count=0,
        results=results,
    )
    assert "[q-0]" in md
    assert "miss-0" in md
    assert "['payment']" in md
    assert "['film', 'actor']" in md


def test_render_markdown_report_concludes_prd_044_when_ef_search_neutral() -> None:
    """A vs B Δ < 1%p → ef_search 200 → 100 원복 안전 권고."""
    results = (
        _make_result(key="A", label="Hybrid + ef200", top3=0.97),
        _make_result(key="B", label="Hybrid + ef100", top3=0.97),
        _make_result(key="C", label="Pure + ef200", top3=0.87),
    )
    md = mcr.render_markdown_report(
        git_sha="x",
        timestamp_iso="t",
        embedder_model="m",
        embedder_dimensions=1024,
        dataset_path="p",
        chunk_count=1,
        table_count=1,
        column_count=0,
        results=results,
    )
    assert "원복 안전" in md


def test_render_markdown_report_concludes_prd_044_when_ef_search_helpful() -> None:
    """A vs B Δ ≥ 1%p → ef_search 200 유지 권고 (손실 percent 명시)."""
    results = (
        _make_result(key="A", label="Hybrid + ef200", top3=0.97),
        _make_result(key="B", label="Hybrid + ef100", top3=0.90),
        _make_result(key="C", label="Pure + ef200", top3=0.87),
    )
    md = mcr.render_markdown_report(
        git_sha="x",
        timestamp_iso="t",
        embedder_model="m",
        embedder_dimensions=1024,
        dataset_path="p",
        chunk_count=1,
        table_count=1,
        column_count=0,
        results=results,
    )
    assert "200 유지" in md
    assert "+7.0%p" in md  # 0.97 - 0.90 = 0.07
