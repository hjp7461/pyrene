"""Unit tests for `pyrene_core.observability`.

PRD-006 §6 / PLAN-006 — InMemorySpanExporter assertions for:

  * `configure_logfire(send_to_logfire="never", ...)` is a true no-op for the
    network sink but still emits spans to attached processors.
  * ADR-002 D3 fallback: when `instrument_pydantic_ai` is missing or raises,
    the helper falls back to `instrument_httpx()` and records the reason
    on `InstrumentationStatus`.
  * `instrument_engine(AsyncEngine)` unwraps `.sync_engine` (the SQLAlchemy
    2.x async-hook trap from PRD-006 §3 / PLAN-006).
  * Span name + attribute regression for the 5 instrumentation sites — we
    drive spans from this test (not via the agent) so no DB / model is
    needed; the agent test under pyrene-sql covers the wired-up shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import logfire
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from pyrene_core import (
    SPAN_AGENT_ATTEMPT,
    SPAN_AGENT_RUN,
    SPAN_SCHEMA_INDEX,
    SPAN_SQL_RUN_AGGREGATE,
    SPAN_SQL_RUN_JOIN,
    SPAN_SQL_RUN_SELECT,
    configure_logfire,
    get_instrumentation_status,
    instrument_engine,
)
from pyrene_core.observability import logfire_setup

# --------------------------------------------------------------------------- #
# Fixture: in-memory span exporter wired into Logfire.                         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """Configure Logfire in `send_to_logfire="never"` mode + InMemorySpanExporter."""
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    configure_logfire(
        service_name="pyrene-test",
        send_to_logfire="never",
        additional_span_processors=[processor],
    )
    yield exp
    exp.clear()


def _spans_by_name(exp: InMemorySpanExporter) -> dict[str, list[ReadableSpan]]:
    out: dict[str, list[ReadableSpan]] = {}
    for span in exp.get_finished_spans():
        out.setdefault(span.name, []).append(span)
    return out


# --------------------------------------------------------------------------- #
# 1. configure_logfire is a no-op for the network sink.                        #
# --------------------------------------------------------------------------- #


def test_configure_logfire_never_mode_does_not_require_token(
    exporter: InMemorySpanExporter,
) -> None:
    """No LOGFIRE_TOKEN + send_to_logfire='never' → still configured cleanly."""
    status = get_instrumentation_status()
    assert status.configured is True
    assert status.send_mode == "never"


def test_configure_logfire_emits_spans_to_processor(
    exporter: InMemorySpanExporter,
) -> None:
    """A single `logfire.span(...)` lands in the InMemorySpanExporter."""
    with logfire.span(SPAN_SQL_RUN_SELECT, table="public.film", limit=5) as span:
        span.set_attribute("row_count", 1)
        span.set_attribute("truncated", False)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    captured = spans[0]
    assert captured.name == SPAN_SQL_RUN_SELECT
    assert captured.attributes is not None
    assert captured.attributes["table"] == "public.film"
    assert captured.attributes["limit"] == 5
    assert captured.attributes["row_count"] == 1
    assert captured.attributes["truncated"] is False


# --------------------------------------------------------------------------- #
# 2. ADR-002 D3 fallback path.                                                 #
# --------------------------------------------------------------------------- #


def test_instrument_pydantic_ai_missing_falls_back_to_httpx() -> None:
    """If `logfire.instrument_pydantic_ai` is absent, httpx fallback runs."""
    # We simulate the broken attribute via patch.object on the logfire module;
    # `getattr(logfire, "instrument_pydantic_ai", None)` returns None and the
    # configure helper should mark the fallback path.
    with patch.object(logfire, "instrument_pydantic_ai", None):
        # Calling configure_logfire with the broken attribute should still
        # succeed and record the fallback in status.
        status = configure_logfire(
            service_name="pyrene-test-fallback",
            send_to_logfire="never",
        )

    assert status.configured is True
    assert status.pydantic_ai_instrumented is False
    assert status.httpx_fallback is True
    assert status.fallback_reason is not None
    assert "instrument_pydantic_ai" in status.fallback_reason


def test_instrument_pydantic_ai_runtime_error_falls_back() -> None:
    """If `instrument_pydantic_ai()` raises, the helper logs + falls back."""

    def boom() -> None:
        raise RuntimeError("simulated 1.93 ↔ 4.0 incompatibility")

    with patch.object(logfire, "instrument_pydantic_ai", boom):
        status = configure_logfire(
            service_name="pyrene-test-runtime",
            send_to_logfire="never",
        )

    assert status.pydantic_ai_instrumented is False
    assert status.httpx_fallback is True
    assert status.fallback_reason is not None
    assert "simulated" in status.fallback_reason


def test_instrument_pydantic_ai_normal_path_records_version() -> None:
    """On the happy path the status carries the pydantic_ai version pin."""
    status = configure_logfire(
        service_name="pyrene-test-happy",
        send_to_logfire="never",
    )
    assert status.pydantic_ai_instrumented is True
    assert status.pydantic_ai_version is not None
    parsed = logfire_setup._parse_version_tuple(status.pydantic_ai_version)
    # ADR-002 D3 floor.
    assert parsed >= (1, 93)


def test_pydantic_ai_below_minimum_triggers_fallback() -> None:
    """A version-check failure also routes to the httpx fallback."""
    fake_version = "1.92.0"
    with patch.object(
        logfire_setup,
        "_check_pydantic_ai_version",
        return_value=(False, fake_version, "version-check failure (test)"),
    ):
        status = configure_logfire(
            service_name="pyrene-test-pin",
            send_to_logfire="never",
        )
    assert status.pydantic_ai_instrumented is False
    assert status.httpx_fallback is True
    assert status.pydantic_ai_version == fake_version
    assert status.fallback_reason == "version-check failure (test)"


# --------------------------------------------------------------------------- #
# 3. SQLAlchemy 2.x async-hook unwrap test.                                    #
# --------------------------------------------------------------------------- #


def test_instrument_engine_async_uses_sync_engine() -> None:
    """`instrument_engine(AsyncEngine)` delegates to logfire.instrument_sqlalchemy
    which (per its docstring) accepts AsyncEngine and unwraps `.sync_engine`
    internally. We assert the helper passes the AsyncEngine through (no
    manual `.sync_engine` call) and increments the engine counter.

    Why we *don't* call `event.listen(engine, "before_execute", fn)` directly:
    SQLAlchemy 2.x silently no-ops that on AsyncEngine — the dispatch fires
    on `engine.sync_engine` only. PRD-006 §3 / PLAN-006 D3.7. We rely on
    `logfire.instrument_sqlalchemy` to do the unwrap, and this test pins the
    contract.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    # asyncpg DSN never connects — `create_async_engine` is lazy, the DBAPI
    # is loaded but no socket is opened until a session executes. asyncpg is
    # already a hard dep of pyrene-sql (which the dev install pulls in via
    # the workspace), so the import succeeds without aiosqlite.
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost/x")

    # Reset baseline + run.
    configure_logfire(
        service_name="pyrene-test-engine",
        send_to_logfire="never",
        instrument_pydantic_ai=False,
    )
    before = get_instrumentation_status().sqlalchemy_engines

    captured: list[Any] = []

    def fake_instrument_sqlalchemy(*, engine: Any, **_kwargs: Any) -> None:
        captured.append(engine)

    with patch.object(
        logfire,
        "instrument_sqlalchemy",
        fake_instrument_sqlalchemy,
    ):
        instrument_engine(engine)

    assert captured == [engine], (
        "instrument_engine must pass the AsyncEngine through to "
        "logfire.instrument_sqlalchemy without manual sync_engine unwrap; "
        "the helper performs the unwrap internally."
    )
    after = get_instrumentation_status().sqlalchemy_engines
    assert after == before + 1


# --------------------------------------------------------------------------- #
# 4. 5-site span name regression — drive spans manually so this test stays    #
#    self-contained (the wired-up agent test runs in pyrene-sql).             #
# --------------------------------------------------------------------------- #


def test_all_five_span_names_appear(exporter: InMemorySpanExporter) -> None:
    """All five PLAN-006 instrumentation sites emit spans under the prefix."""
    sites: list[tuple[str, dict[str, Any]]] = [
        (SPAN_AGENT_RUN, {"model": "anthropic:claude-sonnet-4-6"}),
        (SPAN_SQL_RUN_SELECT, {"table": "public.film", "limit": 5}),
        (SPAN_SQL_RUN_JOIN, {"left_table": "public.payment", "join_type": "INNER"}),
        (SPAN_SQL_RUN_AGGREGATE, {"base_table": "public.payment"}),
        (SPAN_SCHEMA_INDEX, {"connection_id": "test-conn"}),
    ]
    for name, attrs in sites:
        with logfire.span(name, **attrs):
            pass

    captured_names = {s.name for s in exporter.get_finished_spans()}
    expected = {name for name, _ in sites}
    missing = expected - captured_names
    assert not missing, f"spans missing from exporter: {missing}"


# --------------------------------------------------------------------------- #
# 5. nested attempt span parent_span_id assertion (PRD-006 §6).                #
# --------------------------------------------------------------------------- #


def test_attempt_span_parent_is_agent_run(
    exporter: InMemorySpanExporter,
) -> None:
    """`pyrene.agent.attempt` must be a child of `pyrene.agent.run`."""
    with logfire.span(SPAN_AGENT_RUN, model="test", attempt_count=2) as run_span:
        run_ctx = run_span.context
        assert run_ctx is not None
        run_span_id = run_ctx.span_id
        run_trace_id = run_ctx.trace_id
        for idx in range(1, 3):
            with logfire.span(SPAN_AGENT_ATTEMPT, attempt_idx=idx):
                pass

    spans = _spans_by_name(exporter)
    assert SPAN_AGENT_RUN in spans
    assert SPAN_AGENT_ATTEMPT in spans
    attempts = spans[SPAN_AGENT_ATTEMPT]
    assert len(attempts) == 2
    for attempt in attempts:
        # Every child span carries the same trace_id as the parent run.
        assert attempt.context is not None
        assert attempt.context.trace_id == run_trace_id
        # And its parent_span_id matches the parent's span_id.
        assert attempt.parent is not None
        assert attempt.parent.span_id == run_span_id

    # And ordering: attempt_idx=1 finishes before attempt_idx=2.
    idxs = [
        a.attributes["attempt_idx"]
        for a in attempts
        if a.attributes is not None
    ]
    assert idxs == [1, 2]


# --------------------------------------------------------------------------- #
# 6. Span attribute keys/types — sanity for PLAN-006 §6 attribute table.       #
# --------------------------------------------------------------------------- #


def test_run_select_span_attributes_present(
    exporter: InMemorySpanExporter,
) -> None:
    """run_select span carries the PRD-006 §6 attribute set."""
    with logfire.span(
        SPAN_SQL_RUN_SELECT,
        table="public.film",
        where="rating = :rating",
        limit=5,
        user_id="00000000-0000-0000-0000-000000000001",
        team_id="00000000-0000-0000-0000-000000000002",
    ) as span:
        span.set_attribute("row_count", 3)
        span.set_attribute("truncated", False)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs is not None
    for required in (
        "table",
        "where",
        "limit",
        "row_count",
        "truncated",
        "user_id",
        "team_id",
    ):
        assert required in attrs, f"required attribute missing: {required}"
    assert isinstance(attrs["row_count"], int)
    assert isinstance(attrs["limit"], int)
    assert isinstance(attrs["table"], str)


def test_status_extra_warnings_when_httpx_unavailable() -> None:
    """If both pydantic_ai *and* httpx hooks are missing, status records it."""
    with (
        patch.object(logfire, "instrument_pydantic_ai", None),
        patch.object(logfire, "instrument_httpx", None),
    ):
        status = configure_logfire(
            service_name="pyrene-test-no-http",
            send_to_logfire="never",
        )
    assert status.pydantic_ai_instrumented is False
    assert status.httpx_fallback is False
    assert any(
        "httpx-fallback-unavailable" in w for w in status.extra_warnings
    )
