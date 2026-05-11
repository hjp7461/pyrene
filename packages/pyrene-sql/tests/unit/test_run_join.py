"""Unit tests for RunJoinInput validation + run_join SQL builder.

PLAN-004 Day 1. These tests verify the input contract (PRD-004 §4) and the
shape of the rendered SQL. The DB-bound execution path is covered by
`tests/integration/test_join_aggregate_db.py`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pyrene_core import OrderBySpec
from pyrene_sql.tools.models import JoinSpec, RunJoinInput
from pyrene_sql.tools.run_join import render_run_join_sql


def _basic_join(join_type: str = "INNER") -> RunJoinInput:
    """Helper: payment INNER/LEFT/RIGHT JOIN customer on customer_id."""
    return RunJoinInput(
        left="public.payment",
        right="public.customer",
        join=JoinSpec(
            table="public.customer",
            on=(("payment.customer_id", "customer.customer_id"),),
            type=join_type,  # type: ignore[arg-type]
        ),
    )


class TestJoinSpecValidation:
    def test_qualified_table_accepted(self) -> None:
        JoinSpec(
            table="public.customer",
            on=(("payment.customer_id", "customer.customer_id"),),
            type="INNER",
        )

    def test_unqualified_table_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"schema\.table"):
            JoinSpec(
                table="customer",
                on=(("payment.customer_id", "customer.customer_id"),),
                type="INNER",
            )

    def test_on_pair_must_be_table_column(self) -> None:
        with pytest.raises(ValidationError, match=r"table\.column"):
            JoinSpec(
                table="public.customer",
                on=(("customer_id", "customer.customer_id"),),
                type="INNER",
            )

    def test_empty_on_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one"):
            JoinSpec(
                table="public.customer",
                on=(),
                type="INNER",
            )

    def test_invalid_join_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JoinSpec(
                table="public.customer",
                on=(("payment.customer_id", "customer.customer_id"),),
                type="FULL",  # type: ignore[arg-type]
            )


class TestRunJoinInputValidation:
    def test_qualified_left_right_accepted(self) -> None:
        _basic_join()

    def test_unqualified_left_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"schema\.table"):
            RunJoinInput(
                left="payment",
                right="public.customer",
                join=JoinSpec(
                    table="public.customer",
                    on=(("payment.customer_id", "customer.customer_id"),),
                    type="INNER",
                ),
            )

    def test_unqualified_right_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RunJoinInput(
                left="public.payment",
                right="customer",
                join=JoinSpec(
                    table="public.customer",
                    on=(("payment.customer_id", "customer.customer_id"),),
                    type="INNER",
                ),
            )

    def test_select_columns_must_be_bare(self) -> None:
        with pytest.raises(ValidationError, match="bare identifier"):
            RunJoinInput(
                left="public.payment",
                right="public.customer",
                join=JoinSpec(
                    table="public.customer",
                    on=(("payment.customer_id", "customer.customer_id"),),
                    type="INNER",
                ),
                select_left=("payment.amount",),
            )

    def test_empty_select_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="None or non-empty"):
            RunJoinInput(
                left="public.payment",
                right="public.customer",
                join=JoinSpec(
                    table="public.customer",
                    on=(("payment.customer_id", "customer.customer_id"),),
                    type="INNER",
                ),
                select_left=(),
            )

    @pytest.mark.parametrize("dangerous", [";", "--", "/*", "*/"])
    def test_where_dangerous_pattern_rejected(self, dangerous: str) -> None:
        with pytest.raises(ValidationError, match="must not contain"):
            RunJoinInput(
                left="public.payment",
                right="public.customer",
                join=JoinSpec(
                    table="public.customer",
                    on=(("payment.customer_id", "customer.customer_id"),),
                    type="INNER",
                ),
                where=f"customer.customer_id = 1 {dangerous} DROP TABLE x",
            )

    def test_limit_above_1000_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RunJoinInput(
                left="public.payment",
                right="public.customer",
                join=JoinSpec(
                    table="public.customer",
                    on=(("payment.customer_id", "customer.customer_id"),),
                    type="INNER",
                ),
                limit=1001,
            )


class TestSqlBuilderShape:
    def test_inner_join_star(self) -> None:
        sql = render_run_join_sql(_basic_join("INNER"))
        assert "INNER JOIN public.customer" in sql
        assert "ON payment.customer_id = customer.customer_id" in sql
        assert sql.startswith("SELECT * FROM public.payment ")
        assert "LIMIT :__pyrene_limit" in sql

    def test_left_join_with_selected_columns(self) -> None:
        inp = RunJoinInput(
            left="public.customer",
            right="public.rental",
            join=JoinSpec(
                table="public.rental",
                on=(("customer.customer_id", "rental.customer_id"),),
                type="LEFT",
            ),
            select_left=("customer_id", "first_name"),
            select_right=("rental_id",),
            where="rental.rental_id IS NULL",
            limit=10,
        )
        sql = render_run_join_sql(inp)
        # Per-side qualification on projected columns:
        assert "customer.customer_id" in sql
        assert "customer.first_name" in sql
        assert "rental.rental_id" in sql
        assert "LEFT JOIN public.rental" in sql
        assert "WHERE rental.rental_id IS NULL" in sql

    def test_right_join_renders_right_keyword(self) -> None:
        sql = render_run_join_sql(_basic_join("RIGHT"))
        # The PRD-004 contract says we don't silently swap to LEFT — Postgres
        # supports RIGHT JOIN natively, so it surfaces in the rendered SQL.
        assert "RIGHT JOIN public.customer" in sql

    def test_order_by_renders(self) -> None:
        inp = RunJoinInput(
            left="public.payment",
            right="public.customer",
            join=JoinSpec(
                table="public.customer",
                on=(("payment.customer_id", "customer.customer_id"),),
                type="INNER",
            ),
            order_by=(OrderBySpec(column="payment.amount", direction="desc"),),
            limit=5,
        )
        sql = render_run_join_sql(inp)
        assert "ORDER BY payment.amount DESC" in sql

    def test_named_params_preserved_through_where(self) -> None:
        # The builder must not interpolate values — they ride the params dict
        # and bind through SQLAlchemy at execute time.
        inp = RunJoinInput(
            left="public.payment",
            right="public.customer",
            join=JoinSpec(
                table="public.customer",
                on=(("payment.customer_id", "customer.customer_id"),),
                type="INNER",
            ),
            where="payment.amount >= :min_amount",
            where_params={"min_amount": 5.0},
        )
        sql = render_run_join_sql(inp)
        assert ":min_amount" in sql
        assert "5.0" not in sql
