"""PRD-046 §4.2 — sync httpx wrapper for `POST /agents/{spec_id}/run-with-trace`.

Mirrors `api_client.py`'s sync httpx + structured-error pattern. The
response is parsed into a local `AnalystRunResult` dataclass instead of
importing `pyrene_agents.schemas` — ADR-019 / F-15 forbid `pyrene_*`
internal imports from this package (HTTP-only boundary).
"""

from __future__ import annotations

import uuid
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
    truncated: bool = False
    analysis: str = ""
    refusal: str | None = None
    attempts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
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
            truncated=bool(payload.get("truncated", False)),
            analysis=str(payload.get("analysis", "")),
            refusal=payload.get("refusal"),
            attempts=attempts,
            audit_id=payload.get("audit_id"),
            cost_usd=payload.get("cost_usd"),
            logfire_trace_url=payload.get("logfire_trace_url"),
        )


def _make_client() -> httpx.Client:
    """Factory split out so tests can patch it with `MockTransport`."""
    return httpx.Client(timeout=httpx.Timeout(30.0))


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _resolve_spec_uuid(
    client: httpx.Client,
    *,
    spec_id: str,
    jwt: str,
    api_base: str,
) -> str:
    """Resolve a spec *name* → its UUID via `GET /agents/specs`.

    PRD-055 / ADR-026: the backend `POST /agents/{spec_id}/run-with-trace`
    takes a UUID path param, but the frontend addresses the canonical
    analyst by name (``"sql_analyst"``). Resolution stays HTTP-only
    (ADR-019 / F-15 boundary — no `pyrene_*` import). A UUID is passed
    through unchanged so direct-UUID callers keep working.
    """
    if _is_uuid(spec_id):
        return spec_id
    headers = {"Authorization": f"Bearer {jwt}"}
    resp = client.get(f"{api_base.rstrip('/')}/agents/specs", headers=headers)
    if resp.status_code >= 400:
        raise AgentRunError(
            f"에이전트 스펙 목록을 조회할 수 없습니다 (HTTP {resp.status_code}) "
            "— 잠시 후 다시 시도하거나 관리자에게 문의하세요",
            status_code=resp.status_code,
        )
    for spec in resp.json():
        if spec.get("name") == spec_id:
            return str(spec["id"])
    raise AgentRunError(
        f"에이전트 스펙 '{spec_id}' 이(가) 등록되지 않았습니다 "
        "— 관리자에게 스펙 등록을 요청하세요"
    )


def run_agent_with_trace(
    *,
    question: str,
    jwt: str,
    api_base: str,
    spec_id: str = "sql_analyst",
) -> AnalystRunResult:
    """POST /agents/{spec_id}/run-with-trace and parse the response.

    ``spec_id`` may be a spec *name* (resolved to its UUID via
    ``GET /agents/specs``, PRD-055 / ADR-026) or a UUID (passed through).
    Network / timeout / 4xx / 5xx failures are wrapped in `AgentRunError`
    so the caller can map them via `friendly_error` (PRD-020 UX 5각형).
    """
    headers = {"Authorization": f"Bearer {jwt}"}
    body = {"question": question}
    try:
        with _make_client() as client:
            resolved = _resolve_spec_uuid(
                client, spec_id=spec_id, jwt=jwt, api_base=api_base
            )
            url = (
                f"{api_base.rstrip('/')}/agents/{resolved}/run-with-trace"
            )
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
