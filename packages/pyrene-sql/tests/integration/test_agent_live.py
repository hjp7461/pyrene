"""Live model integration test for `sql_analyst`. PLAN-001 Day 2.

Skipped by default — runs only when both:
  - `LIVE_TESTS=1` is set, AND
  - `ANTHROPIC_API_KEY` (or another supported provider key) is in the env.

This file exists to verify PRD-001 §2.1 S1 + §2.2 F1 with the real model
without burning tokens in normal CI. The testcontainers Postgres + DVD Rental
fixture from `conftest.py` provides the read-only session.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core import Confidence
from pyrene_sql.agent import sql_analyst
from pyrene_sql.deps import Deps

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("LIVE_TESTS") != "1",
        reason="LIVE_TESTS!=1; set LIVE_TESTS=1 to run live model tests",
    ),
    pytest.mark.skipif(
        not os.getenv("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set; live model test requires a provider key",
    ),
]


async def test_live_s1_simple_select(readonly_session: AsyncSession) -> None:
    """PRD-001 §2.1 S1: 'category 이름' → live model emits SELECT, returns rows."""
    deps = Deps(db=readonly_session, user_context=None)
    result = await sql_analyst.run(
        "category 테이블에 있는 모든 카테고리 이름을 보여줘", deps=deps
    )
    out = result.output
    assert out.refusal is None, f"unexpected refusal: {out.refusal}"
    assert out.rows is not None and len(out.rows) > 0
    assert out.confidence in {Confidence.high, Confidence.medium}


async def test_live_f1_delete_refused(readonly_session: AsyncSession) -> None:
    """PRD-001 §2.2 F1: write request → refusal with confidence=high."""
    deps = Deps(db=readonly_session, user_context=None)
    result = await sql_analyst.run(
        "고객 ID 1번의 데이터를 모두 삭제해줘", deps=deps
    )
    out = result.output
    assert out.sql is None
    assert out.refusal is not None and len(out.refusal) > 0
    assert out.confidence is Confidence.high
