"""PRD-046 §4.2 — `render_attempts_progressively` UX helper.

Sequentially reveals retry attempts in a Streamlit placeholder. `delay_s` is
DI'd so production (≈0.5s for a *thinking* feel) and tests (0.0s,
deterministic) can share the same code path.

Attempts arrive as `dict[str, Any]` (the JSON shape of
`pyrene_sql.retry.AttemptTrace`: `{"sql": str|None, "error": str|None,
"duration_ms": int}`) because the frontend package never imports backend
types — ADR-019 / F-15.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import streamlit as st


def friendly_error(message: str, *, context: str = "재시도") -> str:
    """String-tolerant friendly mapping for attempt errors.

    The api_client.friendly_error signature accepts BaseException; attempts
    arrive as plain strings (already serialized over HTTP). This local
    wrapper renders the error message with a Korean prefix so the user sees
    the same tone as elsewhere in the UI.
    """
    return f"{context} 실패 — {message}"


def render_attempts_progressively(
    attempts: Sequence[dict[str, Any]],
    placeholder: Any,
    delay_s: float = 0.5,
) -> None:
    """Reveal `attempts[0..i]` cumulatively, one per iteration.

    Each iteration redraws the full prefix inside `placeholder.container()`;
    Streamlit replaces the placeholder content each time, which reads to the
    user as *append* with a brief pause between attempts.

    Args:
        attempts: JSON-parsed AttemptTrace dicts (sql / error / duration_ms).
        placeholder: `st.empty()` return value (a DeltaGenerator).
        delay_s: pause between attempts. Production ≈0.5; tests 0.0.
    """
    for i in range(len(attempts)):
        with placeholder.container():
            for j, prior in enumerate(attempts[: i + 1], start=1):
                duration = prior.get("duration_ms", 0)
                st.markdown(f"**Attempt {j}** ({duration}ms)")
                sql = prior.get("sql")
                if sql:
                    st.code(sql, language="sql")
                error = prior.get("error")
                if error:
                    st.error(friendly_error(error))
        if delay_s > 0:
            time.sleep(delay_s)


__all__ = ["friendly_error", "render_attempts_progressively"]
