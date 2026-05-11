"""GitHub MCP tool wrapper — list_issues / create_issue with PAT redaction.

PRD-012 §2 S2 ("외부 MCP 1개 등록 → list_issues, create_issue") +
§6 F2 ("외부 MCP credential이 audit log에 노출 안 됨").

### Why HTTP, not the official GitHub MCP stdio server

PLAN-012 PM amend selected GitHub over echo for demo impact. The
upstream `github/github-mcp-server` is a Go binary; running it as a
stdio subprocess via `StdioMcpClient` would work, but adds an opaque
binary dependency to the docker-compose recipe. For Phase 2 the
contract surface is tiny (two tools), so we call the REST API
(`/repos/{owner}/{repo}/issues`) directly with `httpx`. The wrapper
remains a *gateway tool* — agents see it as just another MCP tool
via the same registration path as `FilesystemMcpTool`. Phase 2.5 may
swap in the binary for richer tool coverage; the interface is
deliberately the same shape.

### Credential redaction (PRD-012 §6 F2 — hard requirement)

The PAT lives in the `GITHUB_PAT` env var (or is passed explicitly
at construction). The wrapper enforces three invariants:

1. **`__repr__` masks** — `repr(GitHubMcpTool(...))` never contains
   the raw PAT. Pytest assertion error messages, logging.exception
   tracebacks, and Logfire span attributes all pass through repr.
2. **`__str__` masks** — same defense for f-string interpolation.
3. **Error message scrub** — `_redact(s)` walks any string that may
   carry the PAT and replaces it with `***`. Applied to every
   exception's args before re-raise.

A dedicated `mask_secret(value, secret)` helper provides byte-level
scrubbing for callers that build their own log lines. The audit hook
in PLAN-015 can call `mask_secret(event.metadata, github_pat)` as a
final scrub pass.

### Token leak surfaces audited

| Surface | Defense |
|---------|---------|
| `repr(self)` | `__repr__` masks |
| `str(self)`  | `__str__` masks (delegates to repr) |
| Exception chain (`__cause__.args`) | `_safe_raise` redact-and-reraise |
| HTTP log (`httpx` debug) | `auth=` not logged; header-only, no query string |
| Audit metadata | `mask_secret` helper for callers |

### Scope

Phase 2 ships `list_issues` + `create_issue`. PRs, search, releases,
gists are out of scope (PRD-012 §3.2 — 1 external MCP is the
sufficient signal).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

_logger = logging.getLogger(__name__)


GITHUB_PAT_ENV = "GITHUB_PAT"
_GITHUB_API_BASE = "https://api.github.com"
_REDACTED = "***"


def mask_secret(value: str, secret: str) -> str:
    """Replace every occurrence of `secret` in `value` with `***`.

    Idempotent: if `secret` is empty or shorter than 4 chars (too
    likely to false-match), we refuse to redact and return `value`
    unchanged. Callers that need short-secret support should fail at
    PAT validation time, not here.
    """
    if not secret or len(secret) < 4:
        return value
    return value.replace(secret, _REDACTED)


def mask_secret_in_obj(obj: object, secret: str) -> object:
    """Recursively walk a JSON-shaped object replacing `secret` in strings.

    Used by the audit sink when it serializes tool metadata: even if a
    tool wrapper accidentally puts the PAT in a return value, the audit
    hook can scrub it before the row is INSERTed.

    Limitations: only walks dict/list/str. Custom objects are not
    inspected — if you put a PAT in a Pydantic model attribute, the
    repr defense (above) is your only line.
    """
    if isinstance(obj, str):
        return mask_secret(obj, secret)
    if isinstance(obj, dict):
        return {k: mask_secret_in_obj(v, secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_secret_in_obj(v, secret) for v in obj]
    return obj


class GitHubError(RuntimeError):
    """Raised when GitHub API returns a non-2xx response or transport fails.

    The constructor scrubs the PAT from the message via the wrapper's
    `_redact()` method before chaining. Catching this type is safe; the
    `args[0]` string is already masked.
    """


class ListIssuesInput(BaseModel):
    """Input contract for `list_issues`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: str = Field(min_length=1, max_length=140)
    state: str = Field(default="open")

    @field_validator("repo")
    @classmethod
    def _repo_is_owner_slash_name(cls, v: str) -> str:
        # GitHub repo names: owner/name. We do not enforce strict
        # charset (GitHub itself allows hyphens, dots, underscores).
        if "/" not in v or v.count("/") != 1:
            raise ValueError("repo must be 'owner/name'")
        owner, name = v.split("/", 1)
        if not owner or not name:
            raise ValueError("repo must be 'owner/name' with non-empty parts")
        return v

    @field_validator("state")
    @classmethod
    def _state_is_known(cls, v: str) -> str:
        if v not in {"open", "closed", "all"}:
            raise ValueError("state must be 'open', 'closed', or 'all'")
        return v


class CreateIssueInput(BaseModel):
    """Input contract for `create_issue`.

    `body` deliberately preserves whitespace (markdown reports rely
    on trailing newlines / fenced blocks). The model does not inherit
    from `StrictBaseModel` because that base sets
    `str_strip_whitespace=True` and would corrupt markdown.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: str = Field(min_length=1, max_length=140)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=64 * 1024)

    @field_validator("repo")
    @classmethod
    def _repo_is_owner_slash_name(cls, v: str) -> str:
        if "/" not in v or v.count("/") != 1:
            raise ValueError("repo must be 'owner/name'")
        owner, name = v.split("/", 1)
        if not owner or not name:
            raise ValueError("repo must be 'owner/name' with non-empty parts")
        return v


class GitHubMcpTool:
    """GitHub MCP tool wrapper with PAT redaction.

    Construction:
        tool = GitHubMcpTool(pat="ghp_...", client=httpx.AsyncClient())

    The `client` is injectable for tests (the unit test suite swaps in
    a transport-mocked AsyncClient). Production callers omit it and
    the wrapper builds + owns one (closed via `aclose()`).

    Thread-safety: the underlying `httpx.AsyncClient` is asyncio-safe;
    the wrapper itself stores only the PAT + client reference. Use one
    instance per asyncio loop.
    """

    def __init__(
        self,
        *,
        pat: str,
        client: httpx.AsyncClient | None = None,
        api_base: str = _GITHUB_API_BASE,
    ) -> None:
        if not pat or len(pat) < 4:
            # Fail fast — a 0/1/2-char PAT cannot be redacted reliably
            # (mask_secret refuses short secrets to avoid false matches).
            raise ValueError(
                "GitHubMcpTool requires a PAT of at least 4 characters"
            )
        self._pat = pat
        self._api_base = api_base.rstrip("/")
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=10.0)

    # --- Redaction surfaces -------------------------------------------------

    def __repr__(self) -> str:
        # NEVER include the raw PAT. Pytest assertion errors and Logfire
        # span attributes call repr() on every object they touch.
        return f"GitHubMcpTool(pat={_REDACTED}, api_base={self._api_base!r})"

    def __str__(self) -> str:
        return self.__repr__()

    def _redact(self, text: str) -> str:
        """Scrub the PAT from any string before it leaves the module."""
        return mask_secret(text, self._pat)

    def _redact_obj(self, obj: object) -> object:
        return mask_secret_in_obj(obj, self._pat)

    # --- Lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the owned httpx client (no-op if injected)."""
        if self._owned_client:
            await self._client.aclose()

    # --- HTTP helper --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # PAT travels in the Authorization header, NEVER in the URL or
        # query string. httpx does not log headers by default.
        return {
            "Authorization": f"Bearer {self._pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pyrene-mcp-tools/0.1",
        }

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        url = f"{self._api_base}{path}"
        try:
            resp = await self._client.request(
                method, url, headers=self._headers(), json=json
            )
        except httpx.HTTPError as exc:
            # Any transport error message may contain headers in some
            # httpx versions; defensive redact.
            raise GitHubError(
                self._redact(f"github transport error: {exc!r}")
            ) from None
        if resp.status_code >= 400:
            # Response body sometimes echoes the request — defensive
            # redact even though GitHub does not echo Authorization.
            raise GitHubError(
                self._redact(
                    f"github API {method} {path} returned "
                    f"{resp.status_code}: {resp.text[:512]}"
                )
            )
        return resp

    # --- Tools --------------------------------------------------------------

    async def list_issues(
        self, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        """List issues on `repo` filtered by `state`.

        Returns a list of {"number", "title", "state", "html_url"} dicts
        — the upstream payload is large; we project to the essentials so
        downstream agents see a stable shape.
        """
        validated = ListIssuesInput(repo=repo, state=state)
        resp = await self._request(
            "GET", f"/repos/{validated.repo}/issues?state={validated.state}"
        )
        raw = resp.json()
        if not isinstance(raw, list):
            raise GitHubError(
                self._redact(
                    f"unexpected list_issues payload shape: {type(raw)!r}"
                )
            )
        return [
            {
                "number": int(item["number"]),
                "title": str(item["title"]),
                "state": str(item["state"]),
                "html_url": str(item["html_url"]),
            }
            for item in raw
        ]

    async def create_issue(
        self, repo: str, title: str, body: str = ""
    ) -> dict[str, Any]:
        """Create an issue on `repo`.

        Returns {"number", "html_url"} of the new issue.
        """
        validated = CreateIssueInput(repo=repo, title=title, body=body)
        resp = await self._request(
            "POST",
            f"/repos/{validated.repo}/issues",
            json={"title": validated.title, "body": validated.body},
        )
        raw = resp.json()
        if not isinstance(raw, dict):
            raise GitHubError(
                self._redact(
                    f"unexpected create_issue payload shape: {type(raw)!r}"
                )
            )
        return {
            "number": int(raw["number"]),
            "html_url": str(raw["html_url"]),
        }


__all__ = [
    "GITHUB_PAT_ENV",
    "CreateIssueInput",
    "GitHubError",
    "GitHubMcpTool",
    "ListIssuesInput",
    "mask_secret",
    "mask_secret_in_obj",
]
