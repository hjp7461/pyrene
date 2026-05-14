"""PRD-046 §4.1 — AnalystResponseWithObservability schema unit tests.

기존 AnalystResponse 의 invariant 보존 + additive 3 필드 검증.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from pyrene_agents.schemas import AnalystResponseWithObservability
from pyrene_core import Confidence
from pyrene_sql.agent import AnalystResponse


def test_subclass_of_analyst_response() -> None:
    """AnalystResponseWithObservability 는 AnalystResponse 의 직접 서브클래스."""
    assert issubclass(AnalystResponseWithObservability, AnalystResponse)


def test_additive_fields_default_none() -> None:
    """3 신규 필드의 default 가 None — 기존 호출자 영향 0."""
    resp = AnalystResponseWithObservability(confidence=Confidence.high)
    assert resp.audit_id is None
    assert resp.cost_usd is None
    assert resp.logfire_trace_url is None


def test_additive_fields_typed() -> None:
    """3 신규 필드 타입 검증."""
    audit = uuid4()
    resp = AnalystResponseWithObservability(
        confidence=Confidence.high,
        audit_id=audit,
        cost_usd=Decimal("0.00123"),
        logfire_trace_url="https://logfire.example/abc",
    )
    assert isinstance(resp.audit_id, UUID)
    assert isinstance(resp.cost_usd, Decimal)
    assert resp.logfire_trace_url is not None
    assert resp.logfire_trace_url.startswith("https://")


def test_inherits_base_fields() -> None:
    """기존 AnalystResponse 의 sql/rows/refusal/attempts/confidence invariant."""
    resp = AnalystResponseWithObservability(
        sql="SELECT 1",
        rows=[{"x": 1}],
        row_count=1,
        analysis="ok",
        confidence=Confidence.high,
    )
    assert resp.sql == "SELECT 1"
    assert resp.rows == [{"x": 1}]
    assert resp.refusal is None
    assert resp.attempts == ()


def test_json_roundtrip() -> None:
    """JSON serialization roundtrip (Decimal → str 변환 invariant)."""
    audit = uuid4()
    resp = AnalystResponseWithObservability(
        confidence=Confidence.medium,
        audit_id=audit,
        cost_usd=Decimal("0.00456"),
        logfire_trace_url="https://logfire.example/xyz",
    )
    js = resp.model_dump_json()
    assert '"cost_usd":"0.00456"' in js
    restored = AnalystResponseWithObservability.model_validate_json(js)
    assert restored.audit_id == audit
    assert restored.cost_usd == Decimal("0.00456")
    assert restored.logfire_trace_url == "https://logfire.example/xyz"
