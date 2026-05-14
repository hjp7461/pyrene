"""Pyrene MCP frontend — Streamlit UI for tool invocation through the gateway.

PRD-040 / PLAN-040. The package mirrors `pyrene-dashboard` patterns
(sync httpx, JWT bearer, fetch_or_stale UX 5각형) but talks ONLY to the
gateway HTTP API — never imports `pyrene-*` internals (ADR-019, F-15).
"""

__all__: tuple[str, ...] = ()
