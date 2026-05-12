"""PRD-031 회귀 가드: 페이지 header 새로 고침 버튼.

5 페이지 (overview, usage, audit, traces, rbac_matrix) 각각의 `st.title()`
옆에 *🔄 새로 고침* 버튼이 있는지 textual scan 으로 검증. Streamlit
런타임 의존성 0 (PRD-021/026 의 idiom, operational-notes §"텍스트 unit
패턴" 참조).
"""

from __future__ import annotations

import re
from pathlib import Path

_PAGES_DIR = Path(__file__).parent.parent / "src" / "pyrene_dashboard" / "pages"


def test_all_pages_have_refresh_button_dashboard() -> None:
    """PRD-031 AC-1: 5 페이지 모두 `🔄 새로 고침` 버튼 존재."""
    pages = [p for p in _PAGES_DIR.glob("*.py") if p.name != "__init__.py"]
    assert len(pages) == 5, f"5 페이지 기대, {len(pages)} 발견: {[p.name for p in pages]}"

    refresh_count_per_page: dict[str, int] = {}
    for page in pages:
        content = page.read_text(encoding="utf-8")
        # button 라벨과 key 가 별도 줄 — 페이지 내 *둘 다 존재* 여부로 판정
        has_label = "🔄 새로 고침" in content
        has_key = re.search(r'key="refresh_[^"]+"', content) is not None
        refresh_count_per_page[page.name] = 1 if (has_label and has_key) else 0

    missing = [name for name, count in refresh_count_per_page.items() if count != 1]
    assert not missing, f"refresh 버튼 미존재 페이지: {missing}, 매트릭스: {refresh_count_per_page}"


def test_refresh_keys_unique_and_prefixed_dashboard() -> None:
    """PRD-031 AC-2: 5 refresh 버튼의 key 가 모두 unique + `refresh_` prefix."""
    keys: list[str] = []
    for page in _PAGES_DIR.glob("*.py"):
        if page.name == "__init__.py":
            continue
        content = page.read_text(encoding="utf-8")
        for line in content.splitlines():
            if "🔄 새로 고침" in line:
                continue  # button line, key is on next line
        for match in re.finditer(r'key="(refresh_[^"]+)"', content):
            keys.append(match.group(1))

    assert len(keys) == 5, f"5 refresh key 기대, {len(keys)} 발견: {keys}"
    assert len(set(keys)) == 5, f"중복 key: {keys}"
    assert all(k.startswith("refresh_") for k in keys), (
        f"refresh_ prefix 위반: {[k for k in keys if not k.startswith('refresh_')]}"
    )


def test_refresh_buttons_call_cache_clear_and_rerun_dashboard() -> None:
    """PRD-031 AC-3/4: refresh 버튼이 cache_data.clear() + bare st.rerun() 호출."""
    cache_clear_count = 0
    bare_rerun_count = 0
    fragment_rerun_in_refresh_count = 0

    for page in _PAGES_DIR.glob("*.py"):
        if page.name == "__init__.py":
            continue
        content = page.read_text(encoding="utf-8")
        lines = content.splitlines()
        in_refresh_block = False
        for line in lines:
            if "🔄 새로 고침" in line:
                in_refresh_block = True
                continue
            if in_refresh_block:
                if "st.cache_data.clear()" in line:
                    cache_clear_count += 1
                elif re.search(r"st\.rerun\(\s*\)", line):
                    bare_rerun_count += 1
                    in_refresh_block = False
                elif 'scope="fragment"' in line:
                    fragment_rerun_in_refresh_count += 1
                    in_refresh_block = False
                elif "def " in line or line.startswith("st."):
                    in_refresh_block = False

    assert cache_clear_count == 5, f"st.cache_data.clear() 5건 기대, {cache_clear_count} 발견"
    assert bare_rerun_count == 5, f"bare st.rerun() 5건 기대, {bare_rerun_count} 발견"
    assert fragment_rerun_in_refresh_count == 0, (
        f"refresh 블록에서 scope=\"fragment\" 발견: {fragment_rerun_in_refresh_count}건 — "
        f"PRD-031 AC-4 위반 (refresh 는 page-level, retry 는 fragment-level)"
    )
