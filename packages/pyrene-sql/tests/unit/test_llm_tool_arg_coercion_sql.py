"""Unit: LLM tool-arg JSON-stringification coercion (PRD-057 / ADR-027 / F-22).

`claude-sonnet-4-6` serializes non-scalar tool-call arguments as JSON
strings (observed: `columns='["category_id", "name"]'`). The structured
tool inputs must accept that encoding (decode at the boundary) while
leaving scalar fields and native inputs untouched.

No DB / LLM / docker. Pure model-validation surface.

Naming: `_coercion_sql` suffix avoids collision with sibling tool tests
(operational-notes 명명 규칙).
"""

from __future__ import annotations

import pytest

from pyrene_core import OrderBySpec
from pyrene_sql.tools.models import (
    AggregationSpec,
    JoinSpec,
    RunAggregateInput,
    RunJoinInput,
)
from pyrene_sql.tools.run_select import RunSelectInput


class TestRunSelectCoercion:
    def test_columns_stringified_array_decoded(self) -> None:
        """The exact PRD-057 observed failure must now parse."""
        inp = RunSelectInput(
            table="public.category",
            columns='["category_id", "name"]',  # type: ignore[arg-type]
            limit=5,
        )
        assert inp.columns == ["category_id", "name"]

    def test_columns_native_array_unchanged(self) -> None:
        inp = RunSelectInput(table="public.film", columns=["title", "rating"])
        assert inp.columns == ["title", "rating"]

    def test_columns_star_literal_unchanged(self) -> None:
        inp = RunSelectInput(table="public.film", columns="*")
        assert inp.columns == "*"

    def test_where_params_stringified_object_decoded(self) -> None:
        inp = RunSelectInput(
            table="public.film",
            columns=["title"],
            where="rating = :r",
            where_params='{"r": "PG"}',  # type: ignore[arg-type]
        )
        assert inp.where_params == {"r": "PG"}

    def test_order_by_stringified_decoded(self) -> None:
        inp = RunSelectInput(
            table="public.film",
            columns=["title"],
            order_by='[{"column": "title", "direction": "desc"}]',  # type: ignore[arg-type]
        )
        assert list(inp.order_by) == [
            OrderBySpec(column="title", direction="desc")
        ]

    def test_scalar_str_field_not_coerced(self) -> None:
        """`where` is a scalar str; a SQL fragment must survive verbatim."""
        inp = RunSelectInput(
            table="public.film",
            columns=["title"],
            where="release_year = 2006",
        )
        assert inp.where == "release_year = 2006"

    def test_invalid_json_string_falls_through_to_validation_error(
        self,
    ) -> None:
        """Non-JSON garbage in a non-scalar field keeps the original
        ValidationError path (no new silent behavior)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RunSelectInput(
                table="public.film",
                columns="category_id, name",  # type: ignore[arg-type]
            )


class TestRunAggregateCoercion:
    def test_group_by_and_aggregations_stringified(self) -> None:
        inp = RunAggregateInput(
            base_table="public.film",
            group_by='["rating"]',  # type: ignore[arg-type]
            aggregations='[{"function": "count", "column": "*"}]',  # type: ignore[arg-type]
        )
        assert tuple(inp.group_by) == ("rating",)
        assert inp.aggregations == (
            AggregationSpec(function="count", column="*"),
        )

    def test_native_aggregate_unchanged(self) -> None:
        inp = RunAggregateInput(
            base_table="public.film",
            group_by=("rating",),
            aggregations=(AggregationSpec(function="count", column="*"),),
        )
        assert tuple(inp.group_by) == ("rating",)


class TestRunJoinCoercion:
    def test_select_left_stringified(self) -> None:
        inp = RunJoinInput(
            left="public.film",
            right="public.language",
            join=JoinSpec(
                table="public.language",
                on=(("film.language_id", "language.language_id"),),
                type="INNER",
            ),
            select_left='["title"]',  # type: ignore[arg-type]
        )
        assert tuple(inp.select_left or ()) == ("title",)

    def test_join_stringified_object_decoded(self) -> None:
        """The whole `join` nested-model field stringified by the LLM."""
        join_str: str = (
            '{"table": "public.language", '
            '"on": [["film.language_id", "language.language_id"]], '
            '"type": "INNER"}'
        )
        inp = RunJoinInput(
            left="public.film",
            right="public.language",
            join=join_str,  # type: ignore[arg-type]
        )
        assert inp.join.table == "public.language"
        assert inp.join.type == "INNER"
