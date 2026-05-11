"""PRD-019 F-4 회귀 가드: UnexpectedModelBehavior wrap.

Pydantic AI `agent.tool(retries=0, ...)` 설정 (builder.py:118) 때문에 LLM이
도구 인자 검증에 실패하면 `UnexpectedModelBehavior`가 raise된다. 외부
`RetryWrapper`는 `PyreneError`만 catch하므로 이 예외를 `ModelToolValidationError`
(RetryableError 서브클래스)로 wrap해야 N1-N4 retry 정책이 적용된다.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

from pyrene_core import ModelToolValidationError, RetryableError
from pyrene_sql import agent as agent_mod
from pyrene_sql.agent import run_with_retry
from pyrene_sql.deps import Deps


def _make_deps() -> Deps:
    return Deps(db=AsyncMock(), user_context=None)


def test_model_tool_validation_error_is_retryable() -> None:
    """클래스 계보 검증 — decide()가 default retry 경로로 분류."""

    err = ModelToolValidationError("tool args invalid")
    assert isinstance(err, RetryableError)


@pytest.mark.asyncio
async def test_unexpected_model_behavior_wrapped_and_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """첫 호출에서 UnexpectedModelBehavior → wrap → 두번째 호출은 성공.

    sql_analyst.run을 직접 monkeypatch하여 첫 호출만 UnexpectedModelBehavior,
    두번째 호출은 정상 AnalystResponse를 반환하도록 한다.
    """
    from pyrene_core import Confidence
    from pyrene_sql.agent import AnalystResponse, sql_analyst

    calls = {"n": 0}

    class _FakeRun:
        def __init__(self, output: AnalystResponse) -> None:
            self.output = output

    async def fake_run(prompt: str, *, deps: Deps) -> _FakeRun:
        calls["n"] += 1
        if calls["n"] == 1:
            raise UnexpectedModelBehavior(
                "Tool 'run_aggregate' exceeded max retries count of 0"
            )
        rows: list[dict[str, Any]] = [{"col": 1}]
        return _FakeRun(
            AnalystResponse(
                sql="SELECT 1",
                rows=rows,
                row_count=1,
                truncated=False,
                analysis="ok",
                confidence=Confidence.high,
            )
        )

    monkeypatch.setattr(sql_analyst, "run", fake_run)

    response = await run_with_retry("dummy question", _make_deps())

    assert calls["n"] == 2, "wrapper가 retry를 한 번 시도해야 함"
    assert response.confidence == Confidence.high
    assert response.row_count == 1
    assert response.attempts is not None
    assert len(response.attempts) == 2
    # 1회차 시도에 ModelToolValidationError가 기록됐는지 확인
    assert response.attempts[0].error is not None
    assert "run_aggregate" in response.attempts[0].error


@pytest.mark.asyncio
async def test_unexpected_model_behavior_exhausts_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모든 시도에서 UnexpectedModelBehavior → 3회 후 refusal 응답."""
    from pyrene_sql.agent import sql_analyst

    calls = {"n": 0}

    async def fake_run(prompt: str, *, deps: Deps) -> Any:
        calls["n"] += 1
        raise UnexpectedModelBehavior(f"persistent failure {calls['n']}")

    monkeypatch.setattr(sql_analyst, "run", fake_run)

    response = await run_with_retry("dummy question", _make_deps())

    assert calls["n"] == 3, "3회까지 시도해야 함"
    assert response.refusal is not None, "exhaust 시 refusal 응답 필요"
    assert response.attempts is not None
    assert len(response.attempts) == 3


@pytest.mark.asyncio
async def test_wrapping_preserves_exception_chain() -> None:
    """ModelToolValidationError가 raised from UnexpectedModelBehavior 체인 유지."""
    original = UnexpectedModelBehavior("orig")
    try:
        raise ModelToolValidationError(str(original)) from original
    except ModelToolValidationError as exc:
        assert exc.__cause__ is original
        assert "orig" in str(exc)


def test_export_visible_from_pyrene_core() -> None:
    """공용 export 경로에서 import 가능해야 한다."""
    # 위 import 자체가 실패하면 ModuleNotFoundError가 났을 것이므로
    # ModelToolValidationError가 pyrene_core 최상위에서 보이는지만 확인.
    import pyrene_core

    assert hasattr(pyrene_core, "ModelToolValidationError")
    assert pyrene_core.ModelToolValidationError is ModelToolValidationError

    # __all__에도 등록되어야 한다 (mypy --strict no_implicit_reexport 호환).
    assert "ModelToolValidationError" in pyrene_core.__all__


# 보강: agent_mod 모듈 export
def test_agent_module_imports_model_tool_validation() -> None:
    """agent.py에서도 ModelToolValidationError 참조 가능."""
    assert hasattr(agent_mod, "ModelToolValidationError")
