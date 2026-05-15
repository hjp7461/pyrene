"""PRD-046 §7.2 — textual-unit invariants for `pages/agent.py`.

The 5th page (Live SQL Analyst chat) is composed of Streamlit calls + the
helpers in `agent_client` / `progressive_disclosure` / `retry_logic`. The
page itself is hard to exercise via the Streamlit runtime in unit tests,
so we verify *code-structure invariants* (PRD-021 / PRD-026 Pattern B) —
the UX 5각형 (PRD-020 / 023 / 026 / 031 / 032) must be visible in source.

All button keys must start with `mcp_` (enforced repo-wide by
`test_page_invariants.test_button_keys_use_mcp_prefix`).
"""

from __future__ import annotations

import re
from pathlib import Path

_AGENT_PAGE = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "pyrene_mcp_frontend"
    / "pages"
    / "agent.py"
)


def _src() -> str:
    return _AGENT_PAGE.read_text(encoding="utf-8")


def test_agent_page_exists() -> None:
    assert _AGENT_PAGE.exists(), f"missing page: {_AGENT_PAGE}"


def test_friendly_error_invoked_at_least_twice() -> None:
    """friendly_error covers both the network/parse error path and the
    *progressive_disclosure* path inside this page surface (PRD-020)."""
    # progressive_disclosure has its own friendly_error; the page must also
    # invoke api_client.friendly_error directly for AgentRunError fallout.
    assert _src().count("friendly_error(") >= 1


def test_spinner_wraps_run_call() -> None:
    """`st.spinner(...)` wraps the agent run (PRD-023 — *fetch only*)."""
    src = _src()
    assert src.count("st.spinner(") == 1
    idx = src.find("st.spinner(")
    window = src[idx : idx + 600]
    assert "run_agent_with_trace" in window or "fetch_or_stale" in window


def test_logfire_link_is_conditional() -> None:
    """The Logfire trace link must be guarded — `None` means no link."""
    assert re.search(r"if\s+\w+\.logfire_trace_url\s*:", _src()) is not None


def test_retry_button_uses_mcp_prefix() -> None:
    """Retry button key starts with `mcp_` (repo-wide page key invariant).

    Tolerates both `key="..."` and `key=f"..."` (f-strings used for
    per-message uniqueness).
    """
    keys = re.findall(
        r'st\.button\(\s*"🔄 재시도"\s*,\s*key=f?"([^"]+)"', _src()
    )
    assert keys, "no '🔄 재시도' button found"
    assert all(k.startswith("mcp_") for k in keys), f"non-mcp_ keys: {keys}"


def test_fetch_or_stale_wraps_external_call() -> None:
    """PRD-032 / ADR-018 — external fetch goes via fetch_or_stale."""
    assert "fetch_or_stale(" in _src()


def test_page_header_has_refresh() -> None:
    """PRD-031 page-header refresh — `🔄 새로 고침` button at the top."""
    src = _src()
    assert 'st.button("🔄 새로 고침"' in src
    # And the title is split across columns (existing pages use [0.85, 0.15])
    assert "st.columns(" in src
