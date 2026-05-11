"""Unit tests for RunSelectInput validation. PLAN-001 Day 1."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pyrene_sql.tools.run_select import RunSelectInput, RunSelectOutput


class TestTableValidation:
    def test_qualified_name_accepted(self) -> None:
        RunSelectInput(table="public.film", columns="*")

    def test_unqualified_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"schema\.table"):
            RunSelectInput(table="film", columns="*")

    def test_uppercase_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RunSelectInput(table="Public.Film", columns="*")

    def test_three_segments_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RunSelectInput(table="db.public.film", columns="*")


class TestColumns:
    def test_star_accepted(self) -> None:
        inp = RunSelectInput(table="public.film", columns="*")
        assert inp.columns == "*"

    def test_list_accepted(self) -> None:
        inp = RunSelectInput(table="public.film", columns=["title", "rental_rate"])
        assert inp.columns == ["title", "rental_rate"]

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            RunSelectInput(table="public.film", columns=[])


class TestLimit:
    def test_default_is_100(self) -> None:
        inp = RunSelectInput(table="public.film", columns="*")
        assert inp.limit == 100

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RunSelectInput(table="public.film", columns="*", limit=0)

    def test_above_1000_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RunSelectInput(table="public.film", columns="*", limit=1001)


class TestWhereGuard:
    def test_clean_fragment_accepted(self) -> None:
        inp = RunSelectInput(
            table="public.film",
            columns="*",
            where="rental_rate >= :min_rate",
            where_params={"min_rate": 4.99},
        )
        assert inp.where_params == {"min_rate": 4.99}

    @pytest.mark.parametrize("dangerous", [";", "--", "/*", "*/"])
    def test_dangerous_pattern_rejected(self, dangerous: str) -> None:
        with pytest.raises(ValidationError, match="must not contain"):
            RunSelectInput(
                table="public.film",
                columns="*",
                where=f"id = 1 {dangerous} DROP TABLE film",
            )


def test_output_basic_shape() -> None:
    out = RunSelectOutput(rows=[{"id": 1}], row_count=1, truncated=False)
    assert out.row_count == 1
    assert not out.truncated
