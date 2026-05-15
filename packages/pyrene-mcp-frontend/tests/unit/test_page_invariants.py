"""Textual-unit invariants for the 6 Streamlit pages.

`code-style.md` §"외부 의존 fetch UX 오각형" requires:
    - every page header has a `🔄 새로 고침` button
    - every external fetch goes through `fetch_or_stale(...)`
    - st.button keys use a consistent prefix (PRD-026 pattern)

These assertions are *textual* (re-readable source code) so they survive
without the Streamlit runtime — same idiom as
`pyrene-dashboard.tests.test_pages_keys_prefix` (PRD-026).
"""

from __future__ import annotations

import re
from pathlib import Path

_PAGES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "pyrene_mcp_frontend"
    / "pages"
)


def _page_files() -> list[Path]:
    return sorted(p for p in _PAGES_DIR.glob("*.py") if p.name != "__init__.py")


_REFRESH_CALL_PATTERN = re.compile(r'st\.button\(\s*"🔄 새로 고침"')


def test_every_page_has_refresh_button() -> None:
    """AC-4 — every page header invokes `st.button("🔄 새로 고침", ...)`."""
    pages = _page_files()
    assert pages, "no page modules found"
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert _REFRESH_CALL_PATTERN.search(text), f"{page.name} 누락"


def test_refresh_button_count_matches_page_count() -> None:
    """Exactly one refresh button call per page (matches widget invocation,
    not docstring/comment occurrences)."""
    pages = _page_files()
    total = sum(
        len(_REFRESH_CALL_PATTERN.findall(p.read_text(encoding="utf-8")))
        for p in pages
    )
    assert total == len(pages)


def test_external_fetch_uses_fetch_or_stale() -> None:
    """AC-3 — pages must NOT call fetch_servers/fetch_tools directly;
    invocations go via fetch_or_stale (UX 5각형).

    Exception: clearing cache (`fetch_servers.clear()`) inside refresh
    button handlers is allowed.
    Exception: invoke_tool is *intentionally* not cached/wrapped (single-shot
    side-effect call protected by `try/except friendly_error`).
    """
    pages = _page_files()
    # At least 3 pages must contain `fetch_or_stale(`
    fetch_or_stale_calls = sum(
        len(re.findall(r"\bfetch_or_stale\s*\(", p.read_text(encoding="utf-8")))
        for p in pages
    )
    assert fetch_or_stale_calls >= 3, (
        f"expected ≥3 fetch_or_stale calls across pages, got "
        f"{fetch_or_stale_calls}"
    )

    for page in pages:
        text = page.read_text(encoding="utf-8")
        # Forbidden direct call patterns (without .clear()):
        for forbidden in ("fetch_servers(token", "fetch_tools(token"):
            offending_lines = [
                line
                for line in text.splitlines()
                if forbidden in line and ".clear()" not in line
            ]
            assert not offending_lines, (
                f"{page.name} calls {forbidden!r} directly: {offending_lines}"
            )


def test_button_keys_use_mcp_prefix() -> None:
    """All `st.button(..., key=...)` keys in pages start with `mcp_`.

    The auth module owns `_login_*` / `_logout_*` keys outside this rule.
    """
    pages = _page_files()
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for key_match in re.finditer(r'st\.button\([^)]*key="([^"]+)"', text):
            key = key_match.group(1)
            assert key.startswith("mcp_"), (
                f"{page.name} has button key {key!r} not starting with 'mcp_'"
            )


def test_page_count_matches_plan() -> None:
    """PRD-040 §3 + PRD-046 + PRD-060 — 6 pages: servers / tool_discovery
    / invoke / trace / agent / cost. `cost.py` 는 records-only 비용
    대시보드 (PRD-060, ADR-029)."""
    expected = {
        "servers.py",
        "tool_discovery.py",
        "invoke.py",
        "trace.py",
        "agent.py",
        "cost.py",
    }
    actual = {p.name for p in _page_files()}
    assert actual == expected, f"page set drift: {actual} vs {expected}"
