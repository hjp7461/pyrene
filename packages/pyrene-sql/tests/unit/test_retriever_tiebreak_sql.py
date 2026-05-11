"""PRD-021 F-1 회귀 가드: PgvectorRetriever top_k SQL 에 secondary ORDER BY.

`ORDER BY embedding <=> :qv` 단독 정렬은 cosine distance tie 시 Postgres
의 physical row order / HNSW 구조에 의존해 환경별 비결정. PRD-021 은
`schema ASC, "table" ASC` 를 secondary ORDER BY 로 추가해 결정성을 회복.

본 모듈은 SQL **텍스트** 가 fix 를 유지하고 있는지를 검증하는 textual unit
가드 — DB 의존성 없음. 통합 동작은
`test_retriever_db.py::test_top_3_accuracy_meets_90_percent_threshold` 가
hard threshold (≥ 90%) 로 검증한다.

왜 textual unit 인가:
  - integration 테스트는 HNSW state 가 다른 테스트와 상호작용하므로 같은
    실행에서 비결정성 도입 위험 (실측 — 본 PRD-021 의 첫 시도에서 발견).
  - secondary ORDER BY 의 *존재* 자체가 회귀 detection 의 핵심 시그널이며,
    이는 SQL string inspection 으로 충분.
"""

from __future__ import annotations

import inspect

from pyrene_sql.schema.retriever import PgvectorRetriever


def test_top_k_sql_has_distance_primary_order() -> None:
    """top_k 의 SQL 이 cosine distance 를 primary order key 로 사용."""
    source = inspect.getsource(PgvectorRetriever.top_k)
    assert "embedding <=> CAST(:qv AS vector)" in source, (
        "primary ORDER BY (cosine distance) 가 SQL 에서 누락됐다 — "
        "retrieval 의미 자체 깨짐"
    )


def test_top_k_sql_has_schema_secondary_order() -> None:
    """PRD-021: secondary ORDER BY 에 schema ASC 포함."""
    source = inspect.getsource(PgvectorRetriever.top_k)
    assert "schema ASC" in source, (
        "PRD-021 F-1 fix 회귀 — schema 단위 secondary ORDER BY 가 빠졌다. "
        "cosine distance tie 시 multi-schema 환경에서 비결정 발생 가능."
    )


def test_top_k_sql_has_table_tertiary_order() -> None:
    """PRD-021: tertiary ORDER BY 에 \"table\" ASC 포함 (예약어 quote)."""
    source = inspect.getsource(PgvectorRetriever.top_k)
    assert '"table" ASC' in source, (
        "PRD-021 F-1 fix 회귀 — table 단위 tertiary ORDER BY 가 빠졌다. "
        "동일 schema 내 cosine distance tie 시 비결정 — CI macOS/Linux 진동 재발."
    )


def test_top_k_sql_order_by_sequence() -> None:
    """ORDER BY 키 순서: distance → schema → table (primary → secondary → tertiary)."""
    source = inspect.getsource(PgvectorRetriever.top_k)
    distance_idx = source.find("embedding <=> CAST(:qv AS vector)")
    schema_idx = source.find("schema ASC")
    table_idx = source.find('"table" ASC')

    assert distance_idx >= 0 and schema_idx >= 0 and table_idx >= 0, (
        "all three ORDER BY keys must be present"
    )
    assert distance_idx < schema_idx < table_idx, (
        f"ORDER BY 키 순서가 어긋남 — distance={distance_idx} schema={schema_idx} "
        f"table={table_idx}. 의미상 distance 가 primary 여야 한다."
    )


def test_top_k_sql_preserves_limit_clause() -> None:
    """LIMIT :k 가 secondary ORDER BY 추가 후에도 유지되는지 (회귀 가드)."""
    source = inspect.getsource(PgvectorRetriever.top_k)
    assert "LIMIT :k" in source, "LIMIT :k 가 SQL 에서 사라졌다"
