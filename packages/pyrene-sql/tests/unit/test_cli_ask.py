"""Unit tests for `pyrene-sql ask`. PLAN-001 Day 3.

We monkeypatch `_run_ask` to return a synthetic `AnalystResponse` so the
tests exercise only the CLI plumbing — JSON output, --pretty rendering,
exit codes. The agent + DB wiring is covered by `test_agent_mock.py` and
the live integration test.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import pyrene_sql.cli as cli_mod
from pyrene_core import Confidence
from pyrene_sql.agent import AnalystResponse


def _fake_response(*, refusal: str | None = None) -> AnalystResponse:
    if refusal is not None:
        return AnalystResponse(
            sql=None,
            rows=None,
            row_count=None,
            truncated=False,
            analysis="",
            confidence=Confidence.high,
            refusal=refusal,
        )
    return AnalystResponse(
        sql="SELECT name FROM public.category LIMIT 5",
        rows=[{"name": "Action"}, {"name": "Animation"}],
        row_count=2,
        truncated=False,
        analysis="Returned two category names.",
        confidence=Confidence.high,
        refusal=None,
    )


@pytest.fixture
def patched_run_ask(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace `_run_ask` with a stub that records the question it received.

    `_run_ask` returns `(AnalystResponse, trace_id | None)` after PLAN-006.
    Tests stub the tuple shape so the CLI plumbing keeps exercising both
    halves of the contract (response render + optional `--trace` echo).
    """
    received: list[str] = []

    async def fake(question: str) -> tuple[AnalystResponse, str | None]:
        received.append(question)
        return _fake_response(), None

    monkeypatch.setattr(cli_mod, "_run_ask", fake)
    return received


def test_ask_json_default(patched_run_ask: list[str]) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["ask", "List 5 film categories"])
    assert result.exit_code == 0, result.output
    assert patched_run_ask == ["List 5 film categories"]
    parsed = json.loads(result.stdout)
    assert parsed["sql"] == "SELECT name FROM public.category LIMIT 5"
    assert parsed["confidence"] == "high"
    assert parsed["row_count"] == 2


def test_ask_pretty_renders_table(patched_run_ask: list[str]) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app, ["ask", "List 5 film categories", "--pretty"]
    )
    assert result.exit_code == 0, result.output
    # The rich table should at least contain the column name + the header
    # labels we render via the summary grid.
    assert "confidence" in result.stdout
    assert "high" in result.stdout
    assert "Action" in result.stdout
    # The JSON braces should NOT be in pretty output.
    assert '"sql"' not in result.stdout


def test_ask_refusal_pretty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(_question: str) -> tuple[AnalystResponse, str | None]:
        return _fake_response(refusal="This system is read-only."), None

    monkeypatch.setattr(cli_mod, "_run_ask", fake)
    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["ask", "delete x", "--pretty"])
    assert result.exit_code == 0, result.output
    assert "refusal" in result.stdout
    assert "read-only" in result.stdout
