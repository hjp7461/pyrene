"""PRD-046 §4.1 — observability metadata helpers for /run-with-trace.

3 helpers, all None-tolerant (ADR-017 partial detection 패턴):
  - lookup_audit_event_id: audit_events SELECT
  - lookup_cost_usd:       usage_records SUM(cost_usd) — multi-attempt 합계
  - build_logfire_trace_url: env-driven URL composition

호출 실패 / row 부재 / env 미설정 시 None 반환. demo endpoint 의
graceful degradation 보장.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_audit.models import AuditEventRow
from pyrene_metering.models import UsageRecord

logger = logging.getLogger(__name__)


async def lookup_audit_event_id(
    session: AsyncSession,
    team_id: UUID,
    request_id: UUID,
) -> UUID | None:
    """audit_events 에서 team_id + request_id 매칭 마지막 row 의 id.

    *순차 보장*: AUDIT hook insert 후 호출됨. 실패/부재 시 None.
    """
    try:
        stmt = (
            select(AuditEventRow.id)
            .where(AuditEventRow.team_id == team_id)
            .where(AuditEventRow.request_id == request_id)
            .order_by(AuditEventRow.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
    except SQLAlchemyError:
        logger.exception(
            "audit_events lookup failed (team=%s req=%s)", team_id, request_id
        )
        return None


async def lookup_cost_usd(
    session: AsyncSession,
    team_id: UUID,
    request_id: UUID,
) -> Decimal | None:
    """usage_records 의 (team_id, request_id) 매칭 모든 attempt cost 합계.

    `UNIQUE(request_id, attempt_idx)` 라 retry 3회면 3 row → SUM 으로
    총 비용. 0건이면 None.
    """
    try:
        stmt = (
            select(func.sum(UsageRecord.cost_usd))
            .where(UsageRecord.team_id == team_id)
            .where(UsageRecord.request_id == request_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
    except SQLAlchemyError:
        logger.exception(
            "usage_records lookup failed (team=%s req=%s)", team_id, request_id
        )
        return None


def build_logfire_trace_url(trace_id: int | None) -> str | None:
    """LOGFIRE_URL env + hex(trace_id) → 공개 URL.

    `pyrene-mcp-frontend.api_client.logfire_trace_url` 의 `/traces/{trace_id}`
    형식과 정합 (server-side 가 OTel int → 32-char hex 변환만 추가). span-level
    deep link 는 Logfire 공개 URL 패턴 미확인 — follow-up 후보.

    LOGFIRE_URL 미설정 / trace_id None (invalid span context) 시 None.
    """
    base = os.getenv("LOGFIRE_URL")
    if not base or trace_id is None:
        return None
    return f"{base.rstrip('/')}/traces/{trace_id:032x}"
