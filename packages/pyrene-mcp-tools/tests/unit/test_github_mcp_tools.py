"""Unit tests for GitHubMcpTool — list/create + PAT redaction.

PRD-012 §6 F2 ("외부 MCP credential이 audit log에 노출 안 됨"). The
redaction tests are the headline — every surface that could leak the
PAT (repr, str, exception chain) is asserted to mask.

Tests use an `httpx.MockTransport` to avoid real network calls.
Test filenames carry the `_mcp_tools` suffix (Wave 8 unique-naming
guard).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pyrene_mcp_tools import (
    GitHubError,
    GitHubMcpTool,
    mask_secret,
    mask_secret_in_obj,
)
from pyrene_mcp_tools.github import CreateIssueInput, ListIssuesInput

_PAT = "ghp_test_secret_token_abc123XYZ"


# --- fixtures ---------------------------------------------------------------


def _make_tool(handler: httpx.MockTransport) -> GitHubMcpTool:
    """Build a tool with an injected transport-mocked client."""
    client = httpx.AsyncClient(transport=handler, base_url="https://example")
    return GitHubMcpTool(pat=_PAT, client=client)


# --- list_issues happy path ------------------------------------------------


async def test_list_issues_projects_payload() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        # PAT MUST travel in Authorization header, not query string.
        assert request.headers["Authorization"] == f"Bearer {_PAT}"
        assert "?state=open" in str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "first",
                    "state": "open",
                    "html_url": "https://github.com/o/r/issues/1",
                    "body": "verbose details we ignore",
                },
                {
                    "number": 2,
                    "title": "second",
                    "state": "open",
                    "html_url": "https://github.com/o/r/issues/2",
                    "body": "...",
                },
            ],
        )

    tool = _make_tool(httpx.MockTransport(_handler))
    try:
        issues = await tool.list_issues("o/r", "open")
    finally:
        await tool.aclose()

    assert issues == [
        {
            "number": 1,
            "title": "first",
            "state": "open",
            "html_url": "https://github.com/o/r/issues/1",
        },
        {
            "number": 2,
            "title": "second",
            "state": "open",
            "html_url": "https://github.com/o/r/issues/2",
        },
    ]


# --- create_issue happy path -----------------------------------------------


async def test_create_issue_posts_payload() -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "number": 42,
                "html_url": "https://github.com/o/r/issues/42",
                "extra": "ignored",
            },
        )

    tool = _make_tool(httpx.MockTransport(_handler))
    try:
        result = await tool.create_issue(
            "o/r", "Bug: ...", body="repro steps"
        )
    finally:
        await tool.aclose()

    assert result == {
        "number": 42,
        "html_url": "https://github.com/o/r/issues/42",
    }
    assert captured["body"] == {"title": "Bug: ...", "body": "repro steps"}


# --- input validation ------------------------------------------------------


def test_list_issues_input_rejects_bad_repo() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        ListIssuesInput(repo="no-slash", state="open")
    with pytest.raises(ValueError, match="owner/name"):
        ListIssuesInput(repo="too/many/slashes", state="open")


def test_list_issues_input_rejects_bad_state() -> None:
    with pytest.raises(ValueError, match="state must be"):
        ListIssuesInput(repo="o/r", state="invalid")


def test_create_issue_input_rejects_empty_title() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CreateIssueInput(repo="o/r", title="", body="x")


# --- PAT redaction — repr / str -------------------------------------------


def test_repr_redacts_pat() -> None:
    tool = GitHubMcpTool(pat=_PAT)
    representation = repr(tool)
    assert _PAT not in representation
    assert "***" in representation


def test_str_redacts_pat() -> None:
    tool = GitHubMcpTool(pat=_PAT)
    assert _PAT not in str(tool)
    assert _PAT not in f"{tool}"


# --- PAT redaction — error surfaces ---------------------------------------


async def test_http_error_scrubs_pat_from_response_body() -> None:
    """If the server (or some upstream proxy) echoes the PAT in its
    response body, the resulting GitHubError must NOT carry the raw
    PAT in args."""

    def _handler(request: httpx.Request) -> httpx.Response:
        # Hypothetical leak: server echoed the auth header into the body.
        return httpx.Response(
            403,
            text=f"forbidden; received token={_PAT}; please rotate",
        )

    tool = _make_tool(httpx.MockTransport(_handler))
    try:
        with pytest.raises(GitHubError) as exc_info:
            await tool.list_issues("o/r")
    finally:
        await tool.aclose()

    message = str(exc_info.value)
    assert _PAT not in message
    assert "***" in message
    # The status code IS still visible (operators need to debug).
    assert "403" in message


async def test_transport_error_scrubs_pat() -> None:
    """If httpx itself raises, the error chain must not carry the PAT."""

    def _handler(request: httpx.Request) -> httpx.Response:
        # Force a transport-level failure that includes the PAT in
        # the message (simulating a misbehaving plugin / proxy).
        raise httpx.ConnectError(
            f"failed to connect; tried with token={_PAT}"
        )

    tool = _make_tool(httpx.MockTransport(_handler))
    try:
        with pytest.raises(GitHubError) as exc_info:
            await tool.list_issues("o/r")
    finally:
        await tool.aclose()

    assert _PAT not in str(exc_info.value)


# --- mask_secret helper ---------------------------------------------------


def test_mask_secret_idempotent() -> None:
    text = f"token={_PAT}"
    once = mask_secret(text, _PAT)
    twice = mask_secret(once, _PAT)
    assert once == twice
    assert _PAT not in once


def test_mask_secret_short_secret_refuses() -> None:
    """A 3-char secret would false-match too aggressively — helper refuses."""
    assert mask_secret("hello world", "wor") == "hello world"
    assert mask_secret("hello world", "") == "hello world"


def test_mask_secret_in_obj_walks_nested() -> None:
    nested = {
        "outer": [
            {"inner": f"see {_PAT} here"},
            "and " + _PAT + " again",
        ],
        "untouched": 42,
    }
    redacted = mask_secret_in_obj(nested, _PAT)
    serialized = json.dumps(redacted)
    assert _PAT not in serialized
    assert serialized.count("***") == 2
    # Non-string values pass through untouched.
    assert isinstance(redacted, dict)
    assert redacted["untouched"] == 42


# --- construction guard ---------------------------------------------------


def test_pat_too_short_rejected() -> None:
    with pytest.raises(ValueError, match="at least 4 characters"):
        GitHubMcpTool(pat="x")
    with pytest.raises(ValueError, match="at least 4 characters"):
        GitHubMcpTool(pat="")
