"""End-to-end span shape test driven through the agent + retry wrapper.

PRD-006 §6 / PLAN-006 — proves the spans we emit from `agent.py` /
`retry.py` carry the correct names, attributes, and parent-child
structure when driven through `run_with_retry`. Uses Pydantic AI's
`FunctionModel` so no real model API is touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pyrene_core import (
    SPAN_AGENT_ATTEMPT,
    SPAN_AGENT_RUN,
    SPAN_SQL_RUN_SELECT,
    Confidence,
    SqlSyntaxError,
    configure_logfire,
)
from pyrene_sql import agent as agent_mod
from pyrene_sql.agent import run_with_retry, sql_analyst
from pyrene_sql.deps import Deps
from pyrene_sql.tools.run_select import RunSelectInput, RunSelectOutput

pytestmark = pytest.mark.asyncio


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    configure_logfire(
        service_name="pyrene-sql-test",
        send_to_logfire="never",
        additional_span_processors=[SimpleSpanProcessor(exp)],
    )
    yield exp
    exp.clear()


def _by_name(spans: list[ReadableSpan]) -> dict[str, list[ReadableSpan]]:
    out: dict[str, list[ReadableSpan]] = {}
    for s in spans:
        out.setdefault(s.name, []).append(s)
    return out


def _make_deps() -> Deps:
    return Deps(db=AsyncMock(), user_context=None)


def _has_tool_return(messages: list[ModelMessage]) -> bool:
    for m in messages:
        for p in getattr(m, "parts", []):
            if getattr(p, "part_kind", "") == "tool-return":
                return True
    return False


def _final_result(**kwargs: Any) -> ToolCallPart:
    return ToolCallPart(tool_name="final_result", args=kwargs)


def _run_select_call(**fields: Any) -> ToolCallPart:
    return ToolCallPart(tool_name="run_select", args={"input": fields})


async def test_happy_path_emits_full_span_tree(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ask → agent.run + agent.attempt + sql.run_select with parent links."""

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
        out = await run_with_retry(
            "category names", _make_deps(), request_id="req-1"
        )

    assert out.confidence is Confidence.high

    spans = _by_name(list(exporter.get_finished_spans()))
    assert SPAN_AGENT_RUN in spans, f"missing {SPAN_AGENT_RUN}"
    assert SPAN_AGENT_ATTEMPT in spans, f"missing {SPAN_AGENT_ATTEMPT}"
    assert SPAN_SQL_RUN_SELECT in spans, f"missing {SPAN_SQL_RUN_SELECT}"

    [run_span] = spans[SPAN_AGENT_RUN]
    [attempt_span] = spans[SPAN_AGENT_ATTEMPT]
    [select_span] = spans[SPAN_SQL_RUN_SELECT]

    assert run_span.context is not None
    assert attempt_span.context is not None
    assert select_span.context is not None

    # parent_span_id of attempt == span_id of agent.run.
    assert attempt_span.parent is not None
    assert attempt_span.parent.span_id == run_span.context.span_id
    # All three share the same trace.
    trace_id = run_span.context.trace_id
    assert attempt_span.context.trace_id == trace_id
    assert select_span.context.trace_id == trace_id

    # Required attributes carried.
    run_attrs = run_span.attributes or {}
    assert run_attrs.get("request_id") == "req-1"
    assert run_attrs.get("attempt_count") == 1
    assert run_attrs.get("outcome") == "success"
    assert run_attrs.get("model")  # agent model name stamped

    attempt_attrs = attempt_span.attributes or {}
    assert attempt_attrs.get("attempt_idx") == 1
    assert attempt_attrs.get("decision") == "success"

    select_attrs = select_span.attributes or {}
    assert select_attrs.get("table") == "public.category"
    assert select_attrs.get("limit") == 5
    assert select_attrs.get("row_count") == 1
    assert select_attrs.get("truncated") is False


async def test_retry_emits_two_attempt_spans_under_one_run(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD-003 S1: column typo retry → two attempt spans, both children of one run."""
    exec_calls: list[RunSelectInput] = []

    async def fake_exec(_session: Any, input: RunSelectInput) -> RunSelectOutput:
        exec_calls.append(input)
        if input.table == "public.films":
            raise SqlSyntaxError(
                "relation 'public.films' does not exist",
                sql="SELECT title FROM public.films LIMIT 5",
            )
        return RunSelectOutput(
            rows=[{"title": "ACADEMY DINOSAUR"}], row_count=1, truncated=False
        )

    monkeypatch.setattr(agent_mod, "execute_run_select", fake_exec)

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if not _has_tool_return(messages):
            text = "".join(
                str(getattr(p, "content", ""))
                for m in messages
                for p in getattr(m, "parts", [])
            )
            if "Previous attempt" in text:
                return ModelResponse(
                    parts=[
                        _run_select_call(
                            table="public.film", columns=["title"], limit=5
                        )
                    ]
                )
            return ModelResponse(
                parts=[
                    _run_select_call(
                        table="public.films", columns=["title"], limit=5
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
                    analysis="ok",
                    confidence="high",
                    refusal=None,
                )
            ]
        )

    with sql_analyst.override(model=FunctionModel(model)):
        out = await run_with_retry("movie title", _make_deps())

    assert len(out.attempts) == 2

    spans = _by_name(list(exporter.get_finished_spans()))
    [run_span] = spans[SPAN_AGENT_RUN]
    attempts = spans[SPAN_AGENT_ATTEMPT]

    assert len(attempts) == 2
    assert run_span.context is not None
    run_span_id = run_span.context.span_id
    for span in attempts:
        assert span.parent is not None
        assert span.parent.span_id == run_span_id

    def _attempt_idx(span: ReadableSpan) -> int:
        value = (span.attributes or {}).get("attempt_idx", 0)
        return int(value) if isinstance(value, (int, float)) else 0

    sorted_attempts = sorted(attempts, key=_attempt_idx)
    a1, a2 = sorted_attempts
    assert (a1.attributes or {}).get("attempt_idx") == 1
    # `RetryDecision.retry` is a StrEnum; str() -> "retry".
    assert (a1.attributes or {}).get("decision") == "retry"
    assert (a1.attributes or {}).get("error_type") == "SqlSyntaxError"
    assert (a2.attributes or {}).get("attempt_idx") == 2
    assert (a2.attributes or {}).get("decision") == "success"
