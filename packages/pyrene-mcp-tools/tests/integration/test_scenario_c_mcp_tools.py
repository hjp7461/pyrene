"""Scenario C — two-MCP hop integration (SQL → Filesystem → GitHub).

PROJECT_BRIEF §3.2 + PRD-012 §2 S3. The demo flow:

    analyst agent  ─SQL→  rows
                   └──── render markdown
                          └────────► FilesystemMcpTool.write_file (report.md)
                          └────────► GitHubMcpTool.create_issue (body=markdown)

This test validates:
  - the dual-MCP hop end-to-end (no real subprocess, no real github),
  - sandbox + redaction defenses STILL hold on the integration path,
  - the data flowing between hops is preserved (no truncation/corruption).

PLAN-009 / DB integration is out of scope here — Wave 8 guard says "no
migrations, MCPServer/MCPTool reuse only". This test stays at the
tool-level interface (FilesystemMcpTool + GitHubMcpTool) and uses
stub SQL rows, because the SQL adapter is PLAN-011's territory.

### Hop security validation

For each hop, we assert:
  - The PAT is never written to the filesystem (would be a catastrophic
    leak if the agent embedded the PAT in the markdown body).
  - The filesystem path is never reflected back to GitHub issue body
    (no sandbox-internals leak to public github.com).
  - The github response is never written back to filesystem (closes
    the loop — bidirectional containment).

Tests use the `_mcp_tools` suffix per Wave 8 unique-naming guard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

# This test does NOT require Docker; it is unit-scope in terms of
# infrastructure but integration-scope in terms of code paths (two
# tool wrappers, real markdown rendering). Skip on Windows.
pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="FilesystemMcpTool requires POSIX O_NOFOLLOW",
    ),
]

from pyrene_mcp_tools import (  # noqa: E402
    FilesystemMcpTool,
    GitHubMcpTool,
    mask_secret_in_obj,
)

_PAT = "ghp_scenario_c_secret_abc123XYZ"


# --- helpers ---------------------------------------------------------------


def _render_markdown(title: str, rows: list[dict[str, Any]]) -> str:
    """Stub markdown renderer — mirrors what an agent would produce
    from `run_aggregate` output. Header + simple table."""
    if not rows:
        return f"# {title}\n\nNo data.\n"
    columns = list(rows[0].keys())
    header = "| " + " | ".join(columns) + " |\n"
    sep = "| " + " | ".join("---" for _ in columns) + " |\n"
    body = "".join(
        "| " + " | ".join(str(r[c]) for c in columns) + " |\n" for r in rows
    )
    return f"# {title}\n\n{header}{sep}{body}"


# --- scenario C — happy path -----------------------------------------------


async def test_scenario_c_sql_to_filesystem_to_github(tmp_path: Path) -> None:
    """End-to-end happy path: SQL rows → markdown → fs write → github issue."""
    # ---- arrange ----
    # 1. Stub SQL rows (would come from `run_aggregate` in real pipeline).
    sql_rows = [
        {"region": "APAC", "revenue": 12_400_000},
        {"region": "EMEA", "revenue": 8_900_000},
        {"region": "NA", "revenue": 21_300_000},
    ]

    # 2. Filesystem MCP tool, sandboxed to tmp_path.
    fs_root = tmp_path / "reports-root"
    fs_root.mkdir()
    fs_tool = FilesystemMcpTool(root=fs_root, allow_write=True)

    # 3. GitHub MCP tool with mock transport.
    github_calls: list[dict[str, Any]] = []

    def _gh_handler(request: httpx.Request) -> httpx.Response:
        # Capture the issue body so we can verify what was sent.
        body = json.loads(request.content) if request.content else {}
        github_calls.append(body)
        return httpx.Response(
            201,
            json={
                "number": 99,
                "html_url": "https://github.com/o/r/issues/99",
            },
        )

    gh_tool = GitHubMcpTool(
        pat=_PAT,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(_gh_handler),
            base_url="https://example",
        ),
    )

    # ---- act ----
    markdown = _render_markdown("Q1 2026 Revenue by Region", sql_rows)

    # Hop 1: filesystem write.
    fs_result = await fs_tool.write_file("q1-2026-revenue.md", markdown)

    # Hop 2: github issue created from same markdown.
    gh_result = await gh_tool.create_issue(
        repo="finops/reports",
        title="Q1 2026 Revenue Report",
        body=markdown,
    )

    await gh_tool.aclose()

    # ---- assert: data integrity ----
    written_path = fs_root / "q1-2026-revenue.md"
    assert written_path.exists()
    assert written_path.read_text() == markdown
    assert fs_result["bytes_written"] == len(markdown.encode("utf-8"))

    assert len(github_calls) == 1
    assert github_calls[0]["body"] == markdown
    assert github_calls[0]["title"] == "Q1 2026 Revenue Report"
    assert gh_result == {
        "number": 99,
        "html_url": "https://github.com/o/r/issues/99",
    }

    # ---- assert: hop security ----
    # 1. PAT never landed in the filesystem.
    assert _PAT not in written_path.read_text()
    # 2. Filesystem absolute path never reflected in github issue body.
    assert str(fs_root) not in github_calls[0]["body"]
    assert str(fs_root) not in github_calls[0]["title"]
    # 3. GitHub URL never landed in filesystem (response was a future state).
    #    (Trivially true in this flow — we never wrote gh_result back.)


# --- scenario C — sandbox holds under malicious SQL output ----------------


async def test_scenario_c_blocks_traversal_in_agent_output(
    tmp_path: Path,
) -> None:
    """If the agent (or prompt injection) tries to write the report to
    `../../etc/passwd`, the sandbox MUST reject — even though the
    markdown body itself is harmless."""
    fs_root = tmp_path / "fs-root"
    fs_root.mkdir()
    fs_tool = FilesystemMcpTool(root=fs_root, allow_write=True)

    from pyrene_mcp_tools import PathTraversalError

    with pytest.raises(PathTraversalError):
        await fs_tool.write_file(
            "../../etc/pyrene-leak", "agent thought this was a fine target"
        )

    # And the original sandbox root has no leaked files.
    assert list(fs_root.iterdir()) == []


# --- scenario C — github error preserves PAT redaction --------------------


async def test_scenario_c_github_failure_keeps_pat_hidden(
    tmp_path: Path,
) -> None:
    """If the github hop fails (rate limit, auth issue), the error
    propagating up to the agent MUST not leak the PAT — even if the
    filesystem hop already succeeded."""
    fs_root = tmp_path / "fs-root"
    fs_root.mkdir()
    fs_tool = FilesystemMcpTool(root=fs_root, allow_write=True)

    # Mock GitHub: return 401 with the PAT echoed in body (worst case).
    def _gh_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text=f"unauthorized; received {_PAT}; rotate immediately",
        )

    gh_tool = GitHubMcpTool(
        pat=_PAT,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(_gh_handler),
            base_url="https://example",
        ),
    )

    # Hop 1 succeeds.
    await fs_tool.write_file("partial-report.md", "# partial\n")

    # Hop 2 fails — error must be scrubbed.
    from pyrene_mcp_tools import GitHubError

    try:
        with pytest.raises(GitHubError) as exc_info:
            await gh_tool.create_issue("o/r", "title", body="body")
        assert _PAT not in str(exc_info.value)
        assert _PAT not in repr(exc_info.value)
    finally:
        await gh_tool.aclose()

    # And the partial filesystem artifact stays inside the sandbox
    # (no orphan files outside).
    assert (fs_root / "partial-report.md").exists()


# --- audit-trail mask helper used at hop boundary -------------------------


def test_scenario_c_audit_metadata_can_be_scrubbed() -> None:
    """PLAN-015 audit hook can pass tool metadata through
    `mask_secret_in_obj(metadata, pat)` as a final defense layer
    before INSERT to the audit_events table."""
    metadata = {
        "tool_name": "github.create_issue",
        "inputs": {"repo": "o/r", "title": "T"},
        # Hypothetical bug: a tool wrapper accidentally echoed the PAT
        # in its return value.
        "result": {"number": 1, "leaked": f"see {_PAT} here"},
    }
    redacted = mask_secret_in_obj(metadata, _PAT)
    serialized = json.dumps(redacted)
    assert _PAT not in serialized
    assert "***" in serialized
