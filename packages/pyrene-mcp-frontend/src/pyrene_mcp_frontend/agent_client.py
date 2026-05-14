"""PRD-046 §4.2 — sync httpx wrapper for `POST /agents/{spec_id}/run-with-trace`.

Mirrors `api_client.py`'s sync httpx + structured-error pattern. The
response is parsed into a local `AnalystRunResult` dataclass instead of
importing `pyrene_agents.schemas` — ADR-019 / F-15 forbid `pyrene_*`
internal imports from this package (HTTP-only boundary).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


class AgentRunError(Exception):
    """Agent run HTTP / network error — `friendly_error` 흡수 대상.

    `status_code` is the HTTP status when the failure is a 4xx/5xx response;
    `None` for network / timeout / parse errors.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AnalystRunResult:
    """Local mirror of `AnalystResponseWithObservability` from the backend.

    Defined locally per ADR-019 / F-15 (no `pyrene_*` imports from frontend).
    Field names + JSON shape MUST match `pyrene_agents.schemas`
    `AnalystResponseWithObservability` 1:1 — drift here means the UI silently
    breaks. The boundary test (`test_no_internal_imports`) enforces the
    import side; field parity is enforced by the integration test on the
    backend.
    """

    confidence: str
    sql: str | None = None
    rows: tuple[dict[str, Any], ...] | None = None
    row_count: int | None = None
    refusal: str | None = None
    error_message: str | None = None
    attempts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    request_id: str | None = None
    audit_id: str | None = None
    cost_usd: str | None = None
    logfire_trace_url: str | None = None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> AnalystRunResult:
        """Tolerant parse — unknown keys ignored, missing optional → None."""
        raw_rows = payload.get("rows")
        rows = tuple(raw_rows) if raw_rows is not None else None
        raw_attempts = payload.get("attempts") or ()
        attempts = tuple(raw_attempts)
        return cls(
            confidence=str(payload.get("confidence", "")),
            sql=payload.get("sql"),
            rows=rows,
            row_count=payload.get("row_count"),
            refusal=payload.get("refusal"),
            error_message=payload.get("error_message"),
            attempts=attempts,
            request_id=payload.get("request_id"),
            audit_id=payload.get("audit_id"),
            cost_usd=payload.get("cost_usd"),
            logfire_trace_url=payload.get("logfire_trace_url"),
        )


def _make_client() -> httpx.Client:
    """Factory split out so tests can patch it with `MockTransport`."""
    return httpx.Client(timeout=httpx.Timeout(30.0))


def run_agent_with_trace(
    *,
    question: str,
    jwt: str,
    api_base: str,
    spec_id: str = "sql_analyst",
) -> AnalystRunResult:
    """POST /agents/{spec_id}/run-with-trace and parse the response.

    Network / timeout / 4xx / 5xx failures are wrapped in `AgentRunError` so
    the caller can map them via `friendly_error` (PRD-020 UX 5각형).
    """
    url = f"{api_base.rstrip('/')}/agents/{spec_id}/run-with-trace"
    headers = {"Authorization": f"Bearer {jwt}"}
    body = {"question": question}
    try:
        with _make_client() as client:
            resp = client.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                raise AgentRunError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )
            return AnalystRunResult.from_json(resp.json())
    except httpx.TimeoutException as exc:
        raise AgentRunError(f"timeout: {exc}") from exc
    except httpx.RequestError as exc:
        raise AgentRunError(f"network error: {exc}") from exc


__all__ = ["AgentRunError", "AnalystRunResult", "run_agent_with_trace"]
