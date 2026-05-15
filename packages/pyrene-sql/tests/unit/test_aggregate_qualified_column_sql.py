"""Unit: run_aggregate JOIN-aggregate qualified column (PRD-058/ADR-028/F-23).

`RunAggregateInput.joins` supports a single JOIN; a join-aggregate's
column must be qualified (`table.column`) to disambiguate. `group_by`
already accepts qualified — `AggregationSpec.column` must too (internal
contract consistency). `alias` stays bare-only.

No DB / LLM / docker. Validator + SQL-render surface only.

Naming: `_qualified_column_sql` suffix avoids sibling collision
(operational-notes 명명 규칙).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pyrene_sql.tools.models import (
    AggregationSpec,
    JoinSpec,
    RunAggregateInput,
)
from pyrene_sql.tools.run_aggregate import render_run_aggregate_sql


class TestAggregationSpecColumn:
    def test_qualified_column_accepted(self) -> None:
        """The PRD-058 observed failure must now validate."""
        spec = AggregationSpec(
            function="count", column="film_category.film_id"
        )
        assert spec.column == "film_category.film_id"

    def test_bare_column_still_accepted(self) -> None:
        assert AggregationSpec(function="sum", column="amount").column == (
            "amount"
        )

    def test_star_still_accepted(self) -> None:
        assert AggregationSpec(function="count", column="*").column == "*"

    def test_three_part_reference_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AggregationSpec(function="count", column="a.b.c")

    def test_injection_shape_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AggregationSpec(function="sum", column="amount; DROP TABLE x")

    def test_alias_stays_bare_only(self) -> None:
        """Qualified alias remains invalid (SQL alias is never qualified)."""
        with pytest.raises(ValidationError):
            AggregationSpec(
                function="count", column="film_category.film_id", alias="x.y"
            )


class TestRenderQualifiedAggregate:
    def test_render_join_aggregate_emits_qualified_column(self) -> None:
        inp = RunAggregateInput(
            base_table="public.film_category",
            joins=(
                JoinSpec(
                    table="public.category",
                    on=(
                        (
                            "film_category.category_id",
                            "category.category_id",
                        ),
                    ),
                    type="INNER",
                ),
            ),
            aggregations=(
                AggregationSpec(
                    function="count",
                    column="film_category.film_id",
                    alias="film_count",
                ),
            ),
            group_by=("category.name",),
        )
        sql = render_run_aggregate_sql(inp)
        assert "COUNT(film_category.film_id) AS film_count" in sql
        assert "INNER JOIN public.category" in sql
        assert "GROUP BY category.name" in sql

    def test_render_bare_aggregate_unchanged(self) -> None:
        inp = RunAggregateInput(
            base_table="public.payment",
            aggregations=(AggregationSpec(function="sum", column="amount"),),
            group_by=("customer_id",),
        )
        sql = render_run_aggregate_sql(inp)
        assert "SUM(amount)" in sql
