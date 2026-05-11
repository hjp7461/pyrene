"""Mock-model tests for run_with_retry. PLAN-003 §30.

Drives the agent with FunctionModel and a monkeypatched executor that
raises PyreneError subclasses on demand. Verifies PRD-003 §2.1 S1 (column
typo -> self-correct) and S3 (3-attempt exhaustion -> low confidence refusal).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pyrene_core import Confidence, SqlSyntaxError
from pyrene_sql import agent as agent_mod
from pyrene_sql.agent import AnalystResponse, run_with_retry, sql_analyst
from pyrene_sql.deps import Deps
from pyrene_sql.tools.run_select import RunSelectInput, RunSelectOutput

pytestmark = pytest.mark.asyncio


def _make_deps() -> Deps:
    return Deps(db=AsyncMock(), user_context=None)


def _final_result(**kwargs: Any) -> ToolCallPart:
    return ToolCallPart(tool_name="final_result", args=kwargs)


def _run_select_call(**fields: Any) -> ToolCallPart:
    return ToolCallPart(tool_name="run_select", args={"input": fields})


def _has_retry_prompt(messages: list[ModelMessage]) -> bool:
    for m in messages:
        for part in getattr(m, "parts", []):
            if getattr(part, "part_kind", "") == "user-prompt":
                content = str(getattr(part, "content", ""))
                if "Previous attempt" in content:
                    return True
    return False


def _has_tool_return(messages: list[ModelMessage]) -> bool:
    for m in messages:
        for p in getattr(m, "parts", []):
            if getattr(p, "part_kind", "") == "tool-return":
                return True
    return False


# S1: column typo on first wrapper attempt, fixed on the retry.
async def test_s1_column_typo_self_corrected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD-003 §2.1 S1: first run hits a SqlSyntaxError, second run succeeds."""
    exec_calls: list[RunSelectInput] = []

    async def fake_exec(_session: Any, input: RunSelectInput) -> RunSelectOutput:
        exec_calls.append(input)
        if input.table == "public.films":
            raise SqlSyntaxError(
                "relation 'public.films' does not exist",
                sql="SELECT title FROM public.films LIMIT 5",
            )
        return RunSelectOutput(
            rows=[{"title": "ACADEMY DINOSAUR"}],
            row_count=1,
            truncated=False,
        )

    monkeypatch.setattr(agent_mod, "execute_run_select", fake_exec)

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if not _has_tool_return(messages):
            # The first wrapper-attempt run sees no "Previous attempt" prompt;
            # the second one does. Use that to pick the table.
            if _has_retry_prompt(messages):
                return ModelResponse(
                    parts=[
                        _run_select_call(
                            table="public.film",
                            columns=["title"],
                            limit=5,
                        )
                    ]
                )
            return ModelResponse(
                parts=[
                    _run_select_call(
                        table="public.films",
                        columns=["title"],
                        limit=5,
                    )
                ]
            )
        return ModelResponse(
            parts=[
                _final_result(
                    sql="SELECT title FROM public.film LIMIT 5",
                    rows=[{"title": "ACADEMY DINOSAUR"}],
                    row_count=1,
                    truncated=False,
                    analysis="Returned one film title.",
                    confidence="high",
                    refusal=None,
                )
            ]
        )

    with sql_analyst.override(model=FunctionModel(model)):
        out = await run_with_retry("영화 제목 보여줘", _make_deps())

    assert isinstance(out, AnalystResponse)
    assert out.confidence is Confidence.high
    assert out.refusal is None
    assert out.row_count == 1
    assert len(out.attempts) == 2
    assert out.attempts[0].error is not None
    assert "films" in out.attempts[0].error
    assert out.attempts[1].error is None
    assert [c.table for c in exec_calls] == ["public.films", "public.film"]


# S3: 3 attempts all fail -> low-confidence refusal.
async def test_s3_three_failures_yields_low_confidence_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD-003 §2.1 S3: wrapper exhausts max_attempts=3 and synthesises refusal."""
    exec_calls: list[RunSelectInput] = []

    async def fake_exec(_session: Any, input: RunSelectInput) -> RunSelectOutput:
        exec_calls.append(input)
        raise SqlSyntaxError(
            f"syntax error #{len(exec_calls)}",
            sql=f"SELECT bogus_{len(exec_calls)} FROM public.film",
        )

    monkeypatch.setattr(agent_mod, "execute_run_select", fake_exec)

    def model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                _run_select_call(
                    table="public.film",
                    columns=["bogus_column"],
                    limit=5,
                )
            ]
        )

    with sql_analyst.override(model=FunctionModel(model)):
        out = await run_with_retry("영화 제목 보여줘", _make_deps())

    assert isinstance(out, AnalystResponse)
    assert out.confidence is Confidence.low
    assert out.refusal is not None
    assert "syntax error" in out.refusal
    assert len(out.attempts) == 3
    for trace in out.attempts:
        assert trace.error is not None
    assert len(exec_calls) == 3


# Happy path: no errors -> attempts has length 1, success traced.
async def test_happy_path_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(_session: Any, _input: RunSelectInput) -> RunSelectOutput:
        return RunSelectOutput(
            rows=[{"name": "Action"}], row_count=1, truncated=False
        )

    monkeypatch.setattr(agent_mod, "execute_run_select", fake_exec)

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if not _has_tool_return(messages):
            return ModelResponse(
                parts=[
                    _run_select_call(
                        table="public.category", columns=["name"], limit=5
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
                    analysis="One row.",
                    confidence="high",
                    refusal=None,
                )
            ]
        )

    with sql_analyst.override(model=FunctionModel(model)):
        out = await run_with_retry("category 이름", _make_deps())

    assert out.confidence is Confidence.high
    assert out.refusal is None
    assert len(out.attempts) == 1
    assert out.attempts[0].error is None
