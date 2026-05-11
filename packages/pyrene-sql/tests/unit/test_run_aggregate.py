"""Unit tests for RunAggregateInput validation + run_aggregate SQL builder.

PLAN-004 Day 2. Verifies:
  - the cross-field validator that requires `group_by` when `aggregations`
    is non-empty (PRD-004 §6 / §2.2 F2),
  - the 0/1 cap on `joins` (PRD-004 §3.2),
  - each of the 5 aggregation functions renders correctly,
  - alias rendering and the `count(*)` special case,
  - `where` fragment guards parity with PRD-001.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pyrene_core import OrderBySpec
from pyrene_sql.tools.models import (
    AggregationSpec,
    JoinSpec,
    RunAggregateInput,
)
from pyrene_sql.tools.run_aggregate import render_run_aggregate_sql


class TestAggregationSpecValidation:
    @pytest.mark.parametrize("fn", ["count", "sum", "avg", "min", "max"])
    def test_all_five_functions_accepted(self, fn: str) -> None:
        AggregationSpec(function=fn, column="amount")  # type: ignore[arg-type]

    def test_invalid_function_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AggregationSpec(
                function="median",  # type: ignore[arg-type]
                column="amount",
            )

    def test_star_column_accepted(self) -> None:
        AggregationSpec(function="count", column="*")

    def test_invalid_column_rejected(self) -> None:
        with pytest.raises(ValidationError, match="bare identifier"):
            AggregationSpec(function="sum", column="amount; DROP TABLE x")

    def test_alias_bare_only(self) -> None:
        with pytest.raises(ValidationError, match="bare identifier"):
            AggregationSpec(function="sum", column="amount", alias="x.y")


class TestRunAggregateInputValidation:
    def test_aggregations_without_group_by_rejected(self) -> None:
        with pytest.raises(ValidationError, match="require at least one group_by"):
            RunAggregateInput(
                base_table="public.payment",
                aggregations=(AggregationSpec(function="sum", column="amount"),),
            )

    def test_aggregations_with_group_by_accepted(self) -> None:
        RunAggregateInput(
            base_table="public.payment",
            group_by=("customer_id",),
            aggregations=(AggregationSpec(function="sum", column="amount"),),
        )

    def test_empty_aggregations_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one entry"):
            RunAggregateInput(
                base_table="public.payment",
                group_by=("customer_id",),
                aggregations=(),
            )

    def test_two_joins_rejected(self) -> None:
        join_a = JoinSpec(
            table="public.rental",
            on=(("payment.rental_id", "rental.rental_id"),),
            type="INNER",
        )
        join_b = JoinSpec(
            table="public.inventory",
            on=(("rental.inventory_id", "inventory.inventory_id"),),
            type="INNER",
        )
        with pytest.raises(ValidationError, match="at most one JOIN"):
            RunAggregateInput(
                base_table="public.payment",
                joins=(join_a, join_b),
                group_by=("rental.customer_id",),
                aggregations=(AggregationSpec(function="sum", column="amount"),),
            )

    def test_one_join_accepted(self) -> None:
        join_a = JoinSpec(
            table="public.rental",
            on=(("payment.rental_id", "rental.rental_id"),),
            type="INNER",
        )
        RunAggregateInput(
            base_table="public.payment",
            joins=(join_a,),
            group_by=("rental.customer_id",),
            aggregations=(AggregationSpec(function="sum", column="amount"),),
        )

    def test_unqualified_base_table_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"schema\.table"):
            RunAggregateInput(
                base_table="payment",
                group_by=("customer_id",),
                aggregations=(AggregationSpec(function="sum", column="amount"),),
            )

    def test_group_by_qualified_or_bare(self) -> None:
        # Both shapes accepted; an invalid shape rejected.
        RunAggregateInput(
            base_table="public.payment",
            group_by=("customer_id", "rental.staff_id"),
            aggregations=(AggregationSpec(function="sum", column="amount"),),
        )
        with pytest.raises(ValidationError, match="bare identifier or"):
            RunAggregateInput(
                base_table="public.payment",
                group_by=("Customer ID",),
                aggregations=(AggregationSpec(function="sum", column="amount"),),
            )

    @pytest.mark.parametrize("dangerous", [";", "--", "/*", "*/"])
    def test_where_dangerous_pattern_rejected(self, dangerous: str) -> None:
        with pytest.raises(ValidationError, match="must not contain"):
            RunAggregateInput(
                base_table="public.payment",
                group_by=("customer_id",),
                aggregations=(AggregationSpec(function="sum", column="amount"),),
                where=f"amount > 0 {dangerous} DROP TABLE x",
            )


class TestAggregateSqlBuilderShape:
    @pytest.mark.parametrize(
        "fn,column,expected",
        [
            ("count", "*", "COUNT(*)"),
            ("count", "amount", "COUNT(amount)"),
            ("sum", "amount", "SUM(amount)"),
            ("avg", "amount", "AVG(amount)"),
            ("min", "amount", "MIN(amount)"),
            ("max", "amount", "MAX(amount)"),
        ],
    )
    def test_each_aggregation_renders(
        self, fn: str, column: str, expected: str
    ) -> None:
        inp = RunAggregateInput(
            base_table="public.payment",
            group_by=("customer_id",),
            aggregations=(
                AggregationSpec(function=fn, column=column),  # type: ignore[arg-type]
            ),
        )
        sql = render_run_aggregate_sql(inp)
        assert expected in sql

    def test_alias_renders(self) -> None:
        inp = RunAggregateInput(
            base_table="public.payment",
            group_by=("customer_id",),
            aggregations=(
                AggregationSpec(function="sum", column="amount", alias="revenue"),
            ),
        )
        sql = render_run_aggregate_sql(inp)
        assert "SUM(amount) AS revenue" in sql

    def test_count_with_alias(self) -> None:
        inp = RunAggregateInput(
            base_table="public.customer",
            group_by=("store_id",),
            aggregations=(
                AggregationSpec(function="count", column="*", alias="n"),
            ),
        )
        sql = render_run_aggregate_sql(inp)
        assert "COUNT(*) AS n" in sql

    def test_non_count_star_rejected_at_build(self) -> None:
        # SUM(*) makes no sense; the executor rejects it (the Pydantic Literal
        # doesn't constrain this combination).
        inp = RunAggregateInput(
            base_table="public.payment",
            group_by=("customer_id",),
            aggregations=(AggregationSpec(function="sum", column="*"),),
        )
        with pytest.raises(ValueError, match="not supported"):
            render_run_aggregate_sql(inp)

    def test_group_by_and_order_by_emitted(self) -> None:
        inp = RunAggregateInput(
            base_table="public.payment",
            group_by=("customer_id",),
            aggregations=(
                AggregationSpec(function="sum", column="amount", alias="revenue"),
            ),
            order_by=(OrderBySpec(column="revenue", direction="desc"),),
            limit=5,
        )
        sql = render_run_aggregate_sql(inp)
        assert "GROUP BY customer_id" in sql
        assert "ORDER BY revenue DESC" in sql
        assert "LIMIT :__pyrene_limit" in sql

    def test_join_clause_renders(self) -> None:
        inp = RunAggregateInput(
            base_table="public.payment",
            joins=(
                JoinSpec(
                    table="public.rental",
                    on=(("payment.rental_id", "rental.rental_id"),),
                    type="INNER",
                ),
            ),
            group_by=("rental.customer_id",),
            aggregations=(
                AggregationSpec(function="sum", column="amount", alias="revenue"),
            ),
        )
        sql = render_run_aggregate_sql(inp)
        assert "FROM public.payment INNER JOIN public.rental" in sql
        assert "ON payment.rental_id = rental.rental_id" in sql
        assert "GROUP BY rental.customer_id" in sql

    def test_named_params_preserved(self) -> None:
        inp = RunAggregateInput(
            base_table="public.payment",
            group_by=("customer_id",),
            aggregations=(AggregationSpec(function="sum", column="amount"),),
            where="amount > :threshold",
            where_params={"threshold": 1.0},
        )
        sql = render_run_aggregate_sql(inp)
        assert ":threshold" in sql
        assert "1.0" not in sql
