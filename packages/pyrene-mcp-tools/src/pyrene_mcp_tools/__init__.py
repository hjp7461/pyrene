"""Pyrene MCP tool wrappers — Filesystem + GitHub (PRD-012 / PLAN-012).

Wave 8 isolated package. Re-uses PLAN-009's `MCPServer`/`MCPTool` registry
and the gateway hook chain unchanged. Two tools ship:

- `FilesystemMcpTool` — in-process sandbox-confined read/write with
  TOCTOU defense (`O_NOFOLLOW`). PRD-012 §6 F1 (path traversal rejection).
- `GitHubMcpTool` — HTTP-backed issue list/create with PAT redaction
  surfaces. PRD-012 §6 F2 (credential not leaked to audit log).

PDF MCP is deferred to Phase 2.5 (PRD-012 amend) — its TOCTOU surface +
library footprint (pandoc / weasyprint) exceed Day 2's budget. Scenario
C ships as "SQL → markdown report → file" using `FilesystemMcpTool`.
"""

from pyrene_mcp_tools.filesystem import (
    FS_ROOT_ENV,
    FilesystemMcpTool,
    FilesystemReadInput,
    FilesystemSandboxError,
    FilesystemWriteInput,
    PathTraversalError,
    SymlinkRaceError,
)
from pyrene_mcp_tools.github import (
    GITHUB_PAT_ENV,
    CreateIssueInput,
    GitHubError,
    GitHubMcpTool,
    ListIssuesInput,
    mask_secret,
    mask_secret_in_obj,
)

__version__ = "0.1.0"

__all__ = [
    "FS_ROOT_ENV",
    "GITHUB_PAT_ENV",
    "CreateIssueInput",
    "FilesystemMcpTool",
    "FilesystemReadInput",
    "FilesystemSandboxError",
    "FilesystemWriteInput",
    "GitHubError",
    "GitHubMcpTool",
    "ListIssuesInput",
    "PathTraversalError",
    "SymlinkRaceError",
    "mask_secret",
    "mask_secret_in_obj",
]
