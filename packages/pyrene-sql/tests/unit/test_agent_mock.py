"""Mock-model tests for the `sql_analyst` agent. PLAN-001 Day 2.

We drive the agent with `pydantic_ai.models.function.FunctionModel` so we can
script the conversation: first model turn issues a `run_select` tool call,
second turn emits the final `AnalystResponse`. The DB-bound tool body is
stubbed via monkeypatching `execute_run_select` — these tests assert the
agent + schema wiring, not the SQL executor (covered in integration tests).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pyrene_core import Confidence
from pyrene_sql import agent as agent_mod
from pyrene_sql.agent import AnalystResponse, sql_analyst
from pyrene_sql.deps import Deps
from pyrene_sql.tools.run_select import RunSelectInput, RunSelectOutput

pytestmark = pytest.mark.asyncio


def _make_deps() -> Deps:
    """Deps with an AsyncMock standing in for the DB session.

    The session is never touched in these tests because we monkeypatch
    `execute_run_select` to a stub that doesn't dereference it.
    """
    return Deps(db=AsyncMock(), user_context=None)


@pytest.fixture
def stub_execute_run_select(
    monkeypatch: pytest.MonkeyPatch,
) -> list[RunSelectInput]:
    """Replace `execute_run_select` with a recording stub.

    Returns a list that captures the `RunSelectInput`s the agent dispatched.
    Tests append-mutate the stub's response by reassigning a closure variable
    via `set_response`; default response is a single-row category sample.
    """
    captured: list[RunSelectInput] = []
    response: dict[str, RunSelectOutput] = {
        "value": RunSelectOutput(
            rows=[{"name": "Action"}],
            row_count=1,
            truncated=False,
        )
    }

    async def fake(_session: Any, input: RunSelectInput) -> RunSelectOutput:
        captured.append(input)
        return response["value"]

    monkeypatch.setattr(agent_mod, "execute_run_select", fake)
    return captured


def _final_result(**kwargs: Any) -> ToolCallPart:
    """Pydantic AI synthesizes a `final_result` tool from `output_type`.
    Calling it with the AnalystResponse fields ends the run."""
    return ToolCallPart(tool_name="final_result", args=kwargs)


def _run_select_call(**fields: Any) -> ToolCallPart:
    """Issue the `run_select` tool with a structured input dict."""
    return ToolCallPart(tool_name="run_select", args={"input": fields})


async def test_normal_select_flow(
    stub_execute_run_select: list[RunSelectInput],
) -> None:
    """Happy path: first turn calls run_select, second turn emits final output."""
    turns: list[int] = []

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        turns.append(len(messages))
        if len(turns) == 1:
            return ModelResponse(
                parts=[
                    _run_select_call(
                        table="public.category",
                        columns=["name"],
                        limit=5,
                    )
                ]
            )
        return ModelResponse(
            parts=[
                _final_result(
                    sql="SELECT name FROM public.category LIMIT 5",
                    rows=[{"name": "Action"}],
                    row_count=1,
                    truncated=False,
                    analysis="One category was returned.",
                    confidence="high",
                    refusal=None,
                )
            ]
        )

    with sql_analyst.override(model=FunctionModel(model)):
        result = await sql_analyst.run(
            "category 이름 5개 보여줘", deps=_make_deps()
        )

    out = result.output
    assert isinstance(out, AnalystResponse)
    assert out.sql == "SELECT name FROM public.category LIMIT 5"
    assert out.rows == [{"name": "Action"}]
    assert out.row_count == 1
    assert out.truncated is False
    assert out.confidence is Confidence.high
    assert out.refusal is None

    # The tool was actually invoked and saw the validated structured input.
    assert len(stub_execute_run_select) == 1
    captured = stub_execute_run_select[0]
    assert captured.table == "public.category"
    assert captured.columns == ["name"]
    assert captured.limit == 5


async def test_delete_request_is_refused_without_tool_call(
    stub_execute_run_select: list[RunSelectInput],
) -> None:
    """F1 (PRD-001 §2.2): write request → refusal, sql=None, no tool call."""

    def model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                _final_result(
                    sql=None,
                    rows=None,
                    row_count=None,
                    truncated=False,
                    analysis="",
                    confidence="high",
                    refusal=(
                        "이 시스템은 읽기 전용입니다. 삭제·수정 작업은 지원하지 "
                        "않습니다. 대신 해당 행을 조회해 보시겠어요?"
                    ),
                )
            ]
        )

    with sql_analyst.override(model=FunctionModel(model)):
        result = await sql_analyst.run(
            "고객 ID 1번의 데이터를 모두 삭제해줘", deps=_make_deps()
        )

    out = result.output
    assert out.sql is None
    assert out.rows is None
    assert out.row_count is None
    assert out.confidence is Confidence.high
    assert out.refusal is not None
    assert "읽기" in out.refusal or "read-only" in out.refusal.lower()

    # Hard guarantee: refusal MUST NOT have called run_select.
    assert stub_execute_run_select == []


async def test_ambiguous_question_records_assumption_with_medium_confidence(
    stub_execute_run_select: list[RunSelectInput],
) -> None:
    """L-03 (PRD-001 §7): ambiguity → confidence=medium + analysis records the assumption."""
    turns: list[int] = []

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        turns.append(len(messages))
        if len(turns) == 1:
            return ModelResponse(
                parts=[
                    _run_select_call(
                        table="public.film",
                        columns=["title", "rental_rate"],
                        order_by=[{"column": "rental_rate", "direction": "desc"}],
                        limit=5,
                    )
                ]
            )
        return ModelResponse(
            parts=[
                _final_result(
                    sql=(
                        "SELECT title, rental_rate FROM public.film "
                        "ORDER BY rental_rate DESC LIMIT 5"
                    ),
                    rows=[{"title": "Action", "rental_rate": "4.99"}],
                    row_count=1,
                    truncated=False,
                    analysis=(
                        "Assumption: 'top movies' = highest rental_rate. "
                        "Switch to a different metric if needed."
                    ),
                    confidence="medium",
                    refusal=None,
                )
            ]
        )

    with sql_analyst.override(model=FunctionModel(model)):
        result = await sql_analyst.run("top movies 알려줘", deps=_make_deps())

    out = result.output
    assert out.confidence is Confidence.medium
    assert "Assumption" in out.analysis
    assert out.refusal is None
    assert len(stub_execute_run_select) == 1
