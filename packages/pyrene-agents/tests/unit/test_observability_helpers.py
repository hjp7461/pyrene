"""PRD-046 §4.1 — observability helpers (audit / cost / logfire) 단위 테스트.

None-tolerant invariant (ADR-017 partial detection 패턴) 검증.
Unit layer — mock AsyncSession 사용, DB 미연결.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

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


def test_build_logfire_trace_url_returns_url_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOGFIRE_URL + trace_id + span_id → URL."""
    monkeypatch.setenv("LOGFIRE_URL", "https://logfire.example")
    got = build_logfire_trace_url(trace_id=0xABC123, span_id=0xDEF456)
    assert got is not None
    assert "abc123" in got
    assert "def456" in got
    assert got.startswith("https://logfire.example/")


def test_build_logfire_trace_url_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOGFIRE_URL unset → None."""
    monkeypatch.delenv("LOGFIRE_URL", raising=False)
    got = build_logfire_trace_url(trace_id=0xABC, span_id=0xDEF)
    assert got is None


def test_build_logfire_trace_url_returns_none_when_ids_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trace_id 또는 span_id None → None (invalid span context 케이스)."""
    monkeypatch.setenv("LOGFIRE_URL", "https://logfire.example")
    assert build_logfire_trace_url(trace_id=None, span_id=0xDEF) is None
    assert build_logfire_trace_url(trace_id=0xABC, span_id=None) is None
