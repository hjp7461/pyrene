"""Unit tests for PRD-042 / ADR-020 Hybrid chunk helpers.

Three pure-function helpers extracted from `retriever.py` so they can be
exercised without a DB:

- `_split_quota(k)` — how `k` is divided between table and column chunks.
- `_merge_by_distance(table_rows, column_rows)` — distance-ASC merge.
- `render_column_chunk_description(...)` — column-level description format.

These belong with the textual invariants in `test_retriever_tiebreak_sql.py`
but live in a separate file so the responsibility boundary stays clean
(numerical logic here, SQL-text invariants there).
"""

from __future__ import annotations

import pytest

from pyrene_sql.schema.indexer import render_column_chunk_description
from pyrene_sql.schema.models import ColumnSpec
from pyrene_sql.schema.retriever import _merge_by_distance, _split_quota

# ---------------------------------------------------------------------- quota


@pytest.mark.parametrize(
    "k,expected_table,expected_column",
    [
        (0, 0, 0),
        (1, 1, 0),
        (3, 1, 2),  # 3 * 2 // 7 = 0, max(1, 0) = 1
        (7, 2, 5),  # default
        (14, 4, 10),
        (100, 28, 72),
    ],
)
def test_split_quota_default_and_extremes(
    k: int, expected_table: int, expected_column: int
) -> None:
    """PRD-042 / OQ-3 split: ~2:5 ratio with `k_table >= 1` when `k >= 1`."""
    assert _split_quota(k) == (expected_table, expected_column)


def test_split_quota_negative_returns_zeros() -> None:
    assert _split_quota(-1) == (0, 0)
    assert _split_quota(-100) == (0, 0)


# --------------------------------------------------------------------- merge


def test_merge_by_distance_orders_across_chunk_types() -> None:
    """`_merge_by_distance` sorts mixed rows by the distance column (idx 5)."""
    table_rows = [
        ("public", "rental", "table", "", "Table: public.rental ...", 0.30),
        ("public", "payment", "table", "", "Table: public.payment ...", 0.10),
    ]
    column_rows = [
        ("public", "payment", "column", "amount", "Column: ...", 0.05),
        ("public", "film", "column", "rating", "Column: ...", 0.20),
    ]
    merged = _merge_by_distance(table_rows, column_rows)
    assert [r[5] for r in merged] == [0.05, 0.10, 0.20, 0.30]


def test_merge_by_distance_empty_inputs() -> None:
    assert _merge_by_distance([], []) == []
    rows = [("public", "x", "table", "", "...", 0.5)]
    assert _merge_by_distance(rows, []) == rows
    assert _merge_by_distance([], rows) == rows


def test_merge_by_distance_stable_secondary_order_on_ties() -> None:
    """Ties on distance break by (schema, table, column_name) ASC — the
    same invariant `_select_by_chunk_type` enforces inside SQL."""
    a = ("public", "alpha", "column", "x", "...", 0.10)
    b = ("public", "beta", "table", "", "...", 0.10)
    c = ("public", "alpha", "column", "y", "...", 0.10)
    merged = _merge_by_distance([b], [a, c])
    assert merged == [a, c, b]


# --------------------------------------------- render_column_chunk_description


def test_render_column_chunk_description_minimal() -> None:
    col = ColumnSpec(
        name="amount", data_type="numeric", is_nullable=False, description=None
    )
    out = render_column_chunk_description(
        schema="public", table="payment", column=col
    )
    assert out == "Column: public.payment.amount (numeric, NOT NULL)"


def test_render_column_chunk_description_with_comment_suffix() -> None:
    col = ColumnSpec(
        name="rating",
        data_type="mpaa_rating",
        is_nullable=True,
        description="MPAA classification (G/PG/PG-13/R/NC-17)",
    )
    out = render_column_chunk_description(
        schema="public", table="film", column=col
    )
    assert out == (
        "Column: public.film.rating (mpaa_rating, NULL) "
        "-- MPAA classification (G/PG/PG-13/R/NC-17)"
    )


def test_render_column_chunk_description_is_single_line() -> None:
    """The render function itself must not introduce newlines — the
    embedder treats each chunk as a single unit; multi-line would dilute
    the cosine signal. (Upstream comments are trusted to be single-line.)"""
    col_no_comment = ColumnSpec(
        name="x", data_type="text", is_nullable=False, description=None
    )
    out_no_comment = render_column_chunk_description(
        schema="s", table="t", column=col_no_comment
    )
    assert "\n" not in out_no_comment

    col_with_comment = ColumnSpec(
        name="x", data_type="text", is_nullable=False, description="single line"
    )
    out_with_comment = render_column_chunk_description(
        schema="s", table="t", column=col_with_comment
    )
    assert "\n" not in out_with_comment
