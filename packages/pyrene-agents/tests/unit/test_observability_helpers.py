"""PRD-046 §4.1 — observability helpers (audit / cost / logfire) 단위 테스트.

None-tolerant invariant (ADR-017 partial detection 패턴) 검증.
Unit layer — mock AsyncSession 사용, DB 미연결.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from pyrene_agents import observability
from pyrene_agents.observability import (
    build_logfire_trace_url,
    lookup_audit_event_id,
    lookup_cost_usd,
)


@pytest.mark.asyncio
async def test_lookup_audit_event_id_returns_uuid_when_present() -> None:
    """audit_events 에 매칭 row 있으면 UUID 반환."""
    expected = uuid4()
    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = expected
    session = MagicMock()
    session.execute = AsyncMock(return_value=result_proxy)

    got = await lookup_audit_event_id(session, uuid4(), uuid4())
    assert got == expected


@pytest.mark.asyncio
async def test_lookup_audit_event_id_returns_none_when_absent() -> None:
    """row 0건 → None."""
    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=result_proxy)

    got = await lookup_audit_event_id(session, uuid4(), uuid4())
    assert got is None


@pytest.mark.asyncio
async def test_lookup_audit_event_id_returns_none_on_db_error() -> None:
    """DB 예외 → None (graceful)."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=SQLAlchemyError("connection lost"))

    got = await lookup_audit_event_id(session, uuid4(), uuid4())
    assert got is None


@pytest.mark.asyncio
async def test_lookup_cost_usd_sums_across_attempts() -> None:
    """usage_records 에 같은 request_id 의 N attempt 가 있으면 cost 합계 반환."""
    expected = Decimal("0.00345")  # 가상 3 attempt 합계
    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = expected
    session = MagicMock()
    session.execute = AsyncMock(return_value=result_proxy)

    got = await lookup_cost_usd(session, uuid4(), uuid4())
    assert got == expected


@pytest.mark.asyncio
async def test_lookup_cost_usd_returns_none_when_absent() -> None:
    """row 0건 → None."""
    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=result_proxy)

    got = await lookup_cost_usd(session, uuid4(), uuid4())
    assert got is None


@pytest.mark.asyncio
async def test_lookup_cost_usd_returns_none_on_db_error() -> None:
    """DB 예외 → None (graceful, audit lookup 와 대칭 보장)."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=SQLAlchemyError("connection lost"))

    got = await lookup_cost_usd(session, uuid4(), uuid4())
    assert got is None


def test_lookup_cost_usd_uses_sum_aggregate() -> None:
    """source 텍스트에 func.sum invariant — mock 으로는 검증 불가능한 *구조 invariant*.

    `UNIQUE(request_id, attempt_idx)` 라 retry 시 N row → 합계가 본 함수의 존재 이유.
    `operational-notes.md` §"텍스트 unit 패턴" Pattern A 사용 (inspect.getsource).
    """
    src = inspect.getsource(observability.lookup_cost_usd)
    assert "func.sum" in src


def test_build_logfire_trace_url_exact_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOGFIRE_URL + trace_id → 정확한 URL 형식 (`/traces/{hex32}`).

    `pyrene-mcp-frontend.api_client.logfire_trace_url` 의 `/traces/{trace_id}`
    패턴과 정합 — 본 함수가 OTel int → 32-char hex 변환 추가.
    """
    monkeypatch.setenv("LOGFIRE_URL", "https://logfire.example")
    got = build_logfire_trace_url(trace_id=0xABC123)
    # 0xABC123 = 6 hex chars, padded to 32 total = 26 zeros + abc123
    assert got == "https://logfire.example/traces/00000000000000000000000000abc123"


def test_build_logfire_trace_url_strips_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """base URL 의 trailing slash 흡수 (rstrip invariant)."""
    monkeypatch.setenv("LOGFIRE_URL", "https://logfire.example/")
    got = build_logfire_trace_url(trace_id=0xABC)
    # 0xABC = 3 hex chars, padded to 32 total = 29 zeros + abc
    assert got == "https://logfire.example/traces/00000000000000000000000000000abc"


def test_build_logfire_trace_url_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOGFIRE_URL unset → None."""
    monkeypatch.delenv("LOGFIRE_URL", raising=False)
    got = build_logfire_trace_url(trace_id=0xABC)
    assert got is None


def test_build_logfire_trace_url_returns_none_when_trace_id_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trace_id None → None (invalid span context 케이스)."""
    monkeypatch.setenv("LOGFIRE_URL", "https://logfire.example")
    assert build_logfire_trace_url(trace_id=None) is None
