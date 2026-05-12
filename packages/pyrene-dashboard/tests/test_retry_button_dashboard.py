"""PRD-026 회귀 가드: retry 버튼 패턴.

11 호출지점에 일관된 retry button 패턴이 적용됐는지 (key prefix, scope,
cache .clear() callable) 정적/동적 검증. 11 호출지점 *존재* 는 AC-1/2/4
의 grep 으로 검증 — 본 unit 은 *패턴 자체의 의미* 를 검증.
"""

from __future__ import annotations

import re
from pathlib import Path

from pyrene_dashboard.api_client import (
    fetch_audit_events,
    fetch_audit_timeline,
    fetch_budget_blocked,
    fetch_data_permissions,
    fetch_denials_last_hour,
    fetch_rbac_matrix,
    fetch_usage_records,
    fetch_usage_summary,
)

_PAGES_DIR = Path(__file__).parent.parent / "src" / "pyrene_dashboard" / "pages"


def test_cache_clear_method_exposed_on_all_fetch_functions_retry() -> None:
    """PRD-026: 11 호출지점이 호출하는 모든 fetch 함수가 `.clear()` 노출.

    Streamlit `@st.cache_data` decorator 가 `.clear()` 메서드를 제공한다는
    명세에 의존. 메서드 부재 시 retry 버튼이 cache invalidate 실패 → 사용자가
    *재시도해도 같은 stale 결과* — silent failure.
    """
    cached_fetches = {
        "fetch_rbac_matrix": fetch_rbac_matrix,
        "fetch_denials_last_hour": fetch_denials_last_hour,
        "fetch_budget_blocked": fetch_budget_blocked,
        "fetch_usage_summary": fetch_usage_summary,
        "fetch_usage_records": fetch_usage_records,
        "fetch_audit_timeline": fetch_audit_timeline,
        "fetch_audit_events": fetch_audit_events,
        "fetch_data_permissions": fetch_data_permissions,
    }
    for name, fn in cached_fetches.items():
        assert hasattr(fn, "clear"), f"{name} 가 .clear() 미노출"
        assert callable(fn.clear), f"{name}.clear 가 callable 아님"


def test_all_retry_keys_use_prefix_retry_dashboard() -> None:
    """PRD-026 AC-2: 모든 retry 버튼 key 가 `retry_` prefix 시작.

    grep 으로 호출지점 수 검증 (AC-1) 외에, key 명명 일관성을 *정적으로* 검증.
    naming convention 깨지면 DevTools 추적성 약화.
    """
    page_files = sorted(_PAGES_DIR.glob("*.py"))
    assert page_files, "pages 디렉토리 비어있음 — 경로 의심"

    key_pattern = re.compile(r'key="([^"]+)"')
    all_retry_keys: list[str] = []

    for page_file in page_files:
        if page_file.name == "__init__.py":
            continue
        content = page_file.read_text(encoding="utf-8")
        # st.button("🔄 재시도", key="...") 블록 식별
        for line in content.splitlines():
            if "🔄 재시도" in line and "key=" in line:
                match = key_pattern.search(line)
                if match:
                    all_retry_keys.append(match.group(1))

    assert len(all_retry_keys) == 11, f"retry 버튼 11건 기대, {len(all_retry_keys)}건 발견"
    assert all(k.startswith("retry_") for k in all_retry_keys), (
        f"retry_ prefix 위반: {[k for k in all_retry_keys if not k.startswith('retry_')]}"
    )


def test_all_retry_blocks_use_fragment_scope_dashboard() -> None:
    """PRD-026 AC-4: 모든 retry 블록이 `scope="fragment"` 사용.

    전체 `st.rerun()` 사용 시 페이지 chrome 깜빡임 → fragment 의 영역 격리
    의도 무효화. 본 unit 은 *모든 retry 블록 이후 rerun 호출* 이 scope=fragment
    인지 검증.
    """
    page_files = sorted(_PAGES_DIR.glob("*.py"))
    fragment_rerun_count = 0
    bare_rerun_in_retry_count = 0

    for page_file in page_files:
        if page_file.name == "__init__.py":
            continue
        content = page_file.read_text(encoding="utf-8")
        lines = content.splitlines()
        in_retry_block = False
        for line in lines:
            if "🔄 재시도" in line:
                in_retry_block = True
                continue
            if in_retry_block:
                if "st.rerun(scope=\"fragment\")" in line:
                    fragment_rerun_count += 1
                    in_retry_block = False
                elif re.search(r"st\.rerun\(\s*\)", line):
                    bare_rerun_in_retry_count += 1
                    in_retry_block = False
                elif "if st.button" in line or "def " in line:
                    in_retry_block = False

    assert fragment_rerun_count == 11, (
        f"scope=\"fragment\" 11건 기대, {fragment_rerun_count}건 발견"
    )
    assert bare_rerun_in_retry_count == 0, (
        f"retry 블록에서 bare st.rerun() 발견: {bare_rerun_in_retry_count}건 — scope=fragment 누락"
    )
