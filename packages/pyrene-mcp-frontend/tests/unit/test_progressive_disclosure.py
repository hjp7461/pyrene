"""PRD-046 §4.2 — `render_attempts_progressively` unit tests.

`delay_s` is DI'd so tests run with 0.0 (no real sleep). Attempts are dicts
(matching the JSON shape of `pyrene_sql.retry.AttemptTrace`) — the frontend
package never imports the backend type per ADR-019 / F-15.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pyrene_mcp_frontend.progressive_disclosure import (
    render_attempts_progressively,
)


def test_renders_each_attempt_sequentially() -> None:
    """N attempts → N container() calls on the placeholder."""
    attempts = (
        {"sql": "SELECT 1", "error": None, "duration_ms": 10},
        {"sql": "SELECT 2", "error": "syntax error", "duration_ms": 20},
        {"sql": "SELECT 3", "error": None, "duration_ms": 30},
    )
    placeholder = MagicMock()
    placeholder.container.return_value.__enter__.return_value = MagicMock()

    render_attempts_progressively(attempts, placeholder, delay_s=0.0)

    assert placeholder.container.call_count == 3


def test_empty_attempts_no_render() -> None:
    """No attempts → no calls (Streamlit placeholder untouched)."""
    placeholder = MagicMock()
    render_attempts_progressively((), placeholder, delay_s=0.0)
    assert placeholder.container.call_count == 0


def test_friendly_error_invoked_for_error_attempts() -> None:
    """An attempt with `error` field set routes through `friendly_error`."""
    attempts = (
        {"sql": None, "error": "permission denied", "duration_ms": 5},
    )
    placeholder = MagicMock()
    placeholder.container.return_value.__enter__.return_value = MagicMock()

    with patch(
        "pyrene_mcp_frontend.progressive_disclosure.friendly_error"
    ) as fe:
        fe.return_value = "권한 거부"
        render_attempts_progressively(attempts, placeholder, delay_s=0.0)

    fe.assert_called_once()
    args, kwargs = fe.call_args
    assert args[0] == "permission denied" or kwargs.get("exc") == "permission denied"
