"""Pyrene shared abstractions."""

from pyrene_core.audit import AuditEvent, AuditOutcome, AuditSink, _StubAuditSink
from pyrene_core.auth.context import UserContext
from pyrene_core.errors import (
    EmptyResultError,
    ModelToolValidationError,
    NonRetryableError,
    PermissionDeniedError,
    PyreneError,
    QueryTimeoutError,
    RetryableError,
    SqlSyntaxError,
)
from pyrene_core.models import Confidence, OrderBySpec, StrictBaseModel
from pyrene_core.observability import (
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

__version__ = "0.1.0"
__all__ = [
    "SPAN_AGENT_ATTEMPT",
    "SPAN_AGENT_RUN",
    "SPAN_SCHEMA_INDEX",
    "SPAN_SQL_RUN_AGGREGATE",
    "SPAN_SQL_RUN_JOIN",
    "SPAN_SQL_RUN_SELECT",
    "AuditEvent",
    "AuditOutcome",
    "AuditSink",
    "Confidence",
    "EmptyResultError",
    "InstrumentationStatus",
    "ModelToolValidationError",
    "NonRetryableError",
    "OrderBySpec",
    "PermissionDeniedError",
    "PyreneError",
    "QueryTimeoutError",
    "RetryableError",
    "SqlSyntaxError",
    "StrictBaseModel",
    "UserContext",
    "_StubAuditSink",
    "configure_logfire",
    "get_instrumentation_status",
    "instrument_engine",
]
