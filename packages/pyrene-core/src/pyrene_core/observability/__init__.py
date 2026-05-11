"""Observability primitives shared across Pyrene packages.

PRD-006 / PLAN-006. The single public entry point is
`configure_logfire(...)` — call it once at application startup. All other
helpers (span name constants, `instrument_engine`, fallback flags) are
exported for tests and for downstream packages that need to emit traces
under the same naming convention.

Span name convention (PRD-006 §7 L-02):

    pyrene.<phase>.<verb>

Examples:
  - pyrene.agent.run         (top-level agent run, parent of attempts)
  - pyrene.agent.attempt     (one wrapper attempt, child of agent.run)
  - pyrene.sql.run_select    (SELECT tool)
  - pyrene.sql.run_join      (JOIN tool)
  - pyrene.sql.run_aggregate (GROUP BY tool)
  - pyrene.schema.index      (schema indexer)
"""

from __future__ import annotations

from pyrene_core.observability.logfire_setup import (
    SPAN_AGENT_ATTEMPT,
    SPAN_AGENT_RUN,
    SPAN_SCHEMA_INDEX,
    SPAN_SQL_RUN_AGGREGATE,
    SPAN_SQL_RUN_JOIN,
    SPAN_SQL_RUN_SELECT,
    InstrumentationStatus,
    configure_logfire,
    get_instrumentation_status,
    instrument_engine,
)

__all__ = [
    "SPAN_AGENT_ATTEMPT",
    "SPAN_AGENT_RUN",
    "SPAN_SCHEMA_INDEX",
    "SPAN_SQL_RUN_AGGREGATE",
    "SPAN_SQL_RUN_JOIN",
    "SPAN_SQL_RUN_SELECT",
    "InstrumentationStatus",
    "configure_logfire",
    "get_instrumentation_status",
    "instrument_engine",
]
