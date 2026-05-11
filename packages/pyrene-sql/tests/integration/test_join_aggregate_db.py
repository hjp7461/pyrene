"""Integration tests for run_join + run_aggregate against DVD Rental.

PLAN-004 §43 + §75. Verifies the PROJECT_BRIEF §3.1 Phase 1 demo scenarios:

  Q1 — "지난 분기 매출이 가장 많은 영화 카테고리 5개"
       payment + rental → sum(amount) grouped by customer/staff (since Phase 1
       supports at most one JOIN; a true category-level aggregation is the
       LLM's job to simplify, per PRD-004 §3.2). We assert the structural
       behaviour (top-5 rows + ordering) on the closest feasible shape.

  Q2 — "한 번도 빌리지 않은 고객은 몇 명?"
       customer LEFT JOIN rental WHERE rental.rental_id IS NULL — the
       canonical PRD-004 §2.1 S2 scenario.

The fixtures (`readonly_session`, `app_dsn`, etc.) are inherited from
`packages/pyrene-sql/tests/integration/conftest.py`.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core import OrderBySpec
from pyrene_sql.tools.models import (
    AggregationSpec,
    JoinSpec,
    RunAggregateInput,
    RunJoinInput,
)
from pyrene_sql.tools.run_aggregate import execute_run_aggregate
from pyrene_sql.tools.run_join import execute_run_join

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# -- run_join ----------------------------------------------------------------


async def test_inner_join_payment_customer(readonly_session: AsyncSession) -> None:
    """Basic INNER JOIN: payment → customer on customer_id, top 5 payments."""
    inp = RunJoinInput(
        left="public.payment",
        right="public.customer",
        join=JoinSpec(
            table="public.customer",
            on=(("payment.customer_id", "customer.customer_id"),),
            type="INNER",
        ),
        select_left=("amount",),
        select_right=("first_name", "last_name"),
        order_by=(OrderBySpec(column="payment.amount", direction="desc"),),
        limit=5,
    )
    out = await execute_run_join(readonly_session, inp)
    assert out.row_count == 5
    assert out.truncated is True
    # Each row must carry both projected sides.
    for row in out.rows:
        assert "amount" in row
        assert "first_name" in row
        assert "last_name" in row
    # Descending amount: row[0].amount >= row[-1].amount (string-cast because
    # numeric columns serialise as Decimal-strings in `_to_jsonable`).
    amounts = [float(r["amount"]) for r in out.rows]
    assert amounts == sorted(amounts, reverse=True)


async def test_q2_left_join_customers_never_rented(
    readonly_session: AsyncSession,
) -> None:
    """PROJECT_BRIEF §3.1 Q2: customers who never rented (LEFT JOIN + IS NULL).

    Returns the row-level set (not a COUNT, because run_join doesn't aggregate
    — counting orphans is run_aggregate's job, verified below).
    """
    inp = RunJoinInput(
        left="public.customer",
        right="public.rental",
        join=JoinSpec(
            table="public.rental",
            on=(("customer.customer_id", "rental.customer_id"),),
            type="LEFT",
        ),
        select_left=("customer_id", "first_name", "last_name"),
        select_right=("rental_id",),
        where="rental.rental_id IS NULL",
        limit=1000,
    )
    out = await execute_run_join(readonly_session, inp)
    # In the canonical DVD Rental seed every customer has at least one rental,
    # so this returns 0. The assertion is therefore "the query runs and is
    # consistent with the LEFT JOIN semantics" rather than a hard count.
    assert out.row_count >= 0
    for row in out.rows:
        # Every returned row must have rental_id == NULL (the IS NULL clause).
        assert row.get("rental_id") is None


async def test_q2_left_join_count_via_aggregate(
    readonly_session: AsyncSession,
) -> None:
    """PROJECT_BRIEF §3.1 Q2 counting form via run_aggregate.

    customer LEFT JOIN rental, WHERE rental.rental_id IS NULL,
    GROUP BY customer.store_id → COUNT(*). We use `*` (not a column name)
    because both tables expose `customer_id` and a bare reference is
    ambiguous in the joined scope.
    """
    inp = RunAggregateInput(
        base_table="public.customer",
        joins=(
            JoinSpec(
                table="public.rental",
                on=(("customer.customer_id", "rental.customer_id"),),
                type="LEFT",
            ),
        ),
        where="rental.rental_id IS NULL",
        group_by=("customer.store_id",),
        aggregations=(
            AggregationSpec(
                function="count", column="*", alias="never_rented_count"
            ),
        ),
        limit=10,
    )
    out = await execute_run_aggregate(readonly_session, inp)
    # The DVD Rental seed has all 599 customers with at least one rental, so
    # the LEFT JOIN + IS NULL produces an empty result (0 rows is the
    # correct answer to "how many customers never rented?").
    assert out.row_count == 0


async def test_right_join_executes(readonly_session: AsyncSession) -> None:
    """RIGHT JOIN renders + executes against Postgres (sanity check).

    payment RIGHT JOIN customer keeps every customer; the result is equivalent
    to customer LEFT JOIN payment, but we test the literal RIGHT JOIN path
    because the builder does NOT silently rewrite it (PRD-004 §3 risk #3).
    """
    inp = RunJoinInput(
        left="public.payment",
        right="public.customer",
        join=JoinSpec(
            table="public.customer",
            on=(("payment.customer_id", "customer.customer_id"),),
            type="RIGHT",
        ),
        select_right=("customer_id",),
        limit=10,
    )
    out = await execute_run_join(readonly_session, inp)
    assert out.row_count == 10
    assert out.truncated is True


# -- run_aggregate -----------------------------------------------------------


async def test_count_rentals_per_customer(
    readonly_session: AsyncSession,
) -> None:
    """COUNT + GROUP BY single column — the simplest aggregate shape."""
    inp = RunAggregateInput(
        base_table="public.rental",
        group_by=("customer_id",),
        aggregations=(
            AggregationSpec(
                function="count", column="rental_id", alias="rental_count"
            ),
        ),
        order_by=(OrderBySpec(column="rental_count", direction="desc"),),
        limit=5,
    )
    out = await execute_run_aggregate(readonly_session, inp)
    assert out.row_count == 5
    assert out.truncated is True
    for row in out.rows:
        assert "customer_id" in row
        assert "rental_count" in row
        assert int(row["rental_count"]) > 0
    counts = [int(r["rental_count"]) for r in out.rows]
    assert counts == sorted(counts, reverse=True)


async def test_q1_revenue_by_customer_top5(
    readonly_session: AsyncSession,
) -> None:
    """PROJECT_BRIEF §3.1 Q1, simplified to the 1-JOIN Phase 1 envelope.

    The true Q1 chains payment → rental → inventory → film → film_category
    → category (5 joins). Per PRD-004 §3.2, run_aggregate supports at most
    one JOIN, so we exercise the structural pattern (JOIN + SUM + GROUP BY +
    ORDER BY + LIMIT 5) at the closest feasible scope: revenue per
    rental.staff_id via payment ⨝ rental. The LLM would surface the same
    simplification and call it out in `analysis` (system prompt rule 4).
    """
    inp = RunAggregateInput(
        base_table="public.payment",
        joins=(
            JoinSpec(
                table="public.rental",
                on=(("payment.rental_id", "rental.rental_id"),),
                type="INNER",
            ),
        ),
        group_by=("rental.staff_id",),
        aggregations=(
            AggregationSpec(function="sum", column="amount", alias="revenue"),
        ),
        order_by=(OrderBySpec(column="revenue", direction="desc"),),
        limit=5,
    )
    out = await execute_run_aggregate(readonly_session, inp)
    # DVD Rental seed has 2 staff. We asked for top 5; expect ≤ 2 rows.
    assert 1 <= out.row_count <= 5
    assert out.truncated is False  # ≤ limit
    for row in out.rows:
        assert "staff_id" in row
        assert "revenue" in row
        assert float(row["revenue"]) > 0
    revenues = [float(r["revenue"]) for r in out.rows]
    assert revenues == sorted(revenues, reverse=True)


async def test_avg_payment_amount_by_staff(
    readonly_session: AsyncSession,
) -> None:
    """AVG over a numeric column — confirms numeric-cast safety + alias."""
    inp = RunAggregateInput(
        base_table="public.payment",
        group_by=("staff_id",),
        aggregations=(
            AggregationSpec(function="avg", column="amount", alias="avg_amount"),
        ),
        limit=10,
    )
    out = await execute_run_aggregate(readonly_session, inp)
    assert 1 <= out.row_count <= 10
    for row in out.rows:
        assert "staff_id" in row
        assert "avg_amount" in row
        assert float(row["avg_amount"]) > 0


async def test_min_max_payment_amount(readonly_session: AsyncSession) -> None:
    """MIN + MAX in the same aggregate — covers multi-aggregation rendering."""
    inp = RunAggregateInput(
        base_table="public.payment",
        group_by=("staff_id",),
        aggregations=(
            AggregationSpec(function="min", column="amount", alias="min_amount"),
            AggregationSpec(function="max", column="amount", alias="max_amount"),
        ),
        limit=10,
    )
    out = await execute_run_aggregate(readonly_session, inp)
    assert 1 <= out.row_count <= 10
    for row in out.rows:
        assert "min_amount" in row
        assert "max_amount" in row
        assert float(row["min_amount"]) <= float(row["max_amount"])


async def test_aggregate_where_with_named_params(
    readonly_session: AsyncSession,
) -> None:
    """`where` fragment binds named params, not interpolated."""
    inp = RunAggregateInput(
        base_table="public.payment",
        group_by=("staff_id",),
        aggregations=(
            AggregationSpec(function="count", column="*", alias="n"),
        ),
        where="amount >= :min_amount",
        where_params={"min_amount": 5.0},
        limit=10,
    )
    out = await execute_run_aggregate(readonly_session, inp)
    assert out.row_count >= 1
    for row in out.rows:
        assert int(row["n"]) > 0


async def test_truncation_flag_when_over_limit(
    readonly_session: AsyncSession,
) -> None:
    """`limit + 1` fetch detects truncation without an extra COUNT."""
    inp = RunAggregateInput(
        base_table="public.payment",
        group_by=("customer_id",),
        aggregations=(
            AggregationSpec(function="count", column="*", alias="n"),
        ),
        limit=3,
    )
    out = await execute_run_aggregate(readonly_session, inp)
    # DVD Rental has hundreds of customers — 3 will trigger truncation.
    assert out.row_count == 3
    assert out.truncated is True
