"""Logfire configuration + Pydantic AI / SQLAlchemy instrumentation.

PRD-006 §4 entry point + ADR-002 D3 fallback path. Designed so:

- `configure_logfire()` is idempotent — calling it twice is fine. Logfire
  itself dedupes; the global `_STATUS` records what happened so unit tests
  can assert which path ran.
- With `LOGFIRE_TOKEN` unset and `send_to_logfire="if-token-present"`, the
  call is a no-op for the network sink but still installs the OTel tracer
  provider so spans we emit via `logfire.span(...)` are captured by any
  `additional_span_processors` we attach (e.g. an `InMemorySpanExporter` in
  tests). PRD-006 §2.2 F1 — agent never blocks on observability.
- ADR-002 D3 fallback: if `logfire.instrument_pydantic_ai()` is missing on
  the installed `logfire` build (or raises at call time), we still install
  `logfire.instrument_httpx()` so HTTP-level spans for upstream model calls
  remain visible. The fallback path is asserted by unit tests.

Span name constants live here so tools, agent, retry, and indexer all
import the same string. Tests reuse the same constants.
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    # Heavy / optional types pulled in only when type-checking.
    # `SpanProcessor` is exported via opentelemetry.sdk.trace, not
    # opentelemetry.sdk.trace.export — opentelemetry-sdk's `__init__.py`
    # re-exports it from the parent package.
    from opentelemetry.sdk.trace import SpanProcessor
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Span-name constants — re-exported via observability/__init__.py.            #
# All call sites import these by name so that renaming anything ripples       #
# through the test suite immediately.                                          #
# --------------------------------------------------------------------------- #

SPAN_AGENT_RUN: str = "pyrene.agent.run"
SPAN_AGENT_ATTEMPT: str = "pyrene.agent.attempt"
SPAN_SQL_RUN_SELECT: str = "pyrene.sql.run_select"
SPAN_SQL_RUN_JOIN: str = "pyrene.sql.run_join"
SPAN_SQL_RUN_AGGREGATE: str = "pyrene.sql.run_aggregate"
SPAN_SCHEMA_INDEX: str = "pyrene.schema.index"


# Minimum supported Pydantic AI release. ADR-002 D3 — verified via
# `__version__` not feature detection because Pydantic AI's instrument
# attribute names change across minors.
_MIN_PYDANTIC_AI_VERSION: tuple[int, int] = (1, 93)


@dataclass
class InstrumentationStatus:
    """Diagnostic record of `configure_logfire` outcome.

    Tests inspect this to assert which path ran. Production code never
    reads it — but emitting a single `logger.info(status)` in CI is a cheap
    way to catch silent fallbacks during deploys.
    """

    configured: bool = False
    send_mode: str = "never"
    pydantic_ai_instrumented: bool = False
    httpx_fallback: bool = False
    sqlalchemy_engines: int = 0
    pydantic_ai_version: str | None = None
    fallback_reason: str | None = None
    additional_processors: int = 0
    extra_warnings: tuple[str, ...] = field(default_factory=tuple)


_STATUS = InstrumentationStatus()


def get_instrumentation_status() -> InstrumentationStatus:
    """Return the most recent `configure_logfire` outcome (test-only)."""
    return _STATUS


def _parse_version_tuple(raw: str) -> tuple[int, ...]:
    """Best-effort numeric prefix parse: "1.93.0rc1" -> (1, 93, 0)."""
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _check_pydantic_ai_version() -> tuple[bool, str | None, str | None]:
    """Return `(ok, version, reason)`.

    `ok=False` means the installed version is below the minimum supported
    by ADR-002 D3 — the caller falls back to httpx-only instrumentation.
    """
    try:
        import pydantic_ai
    except ImportError as exc:
        return False, None, f"pydantic_ai import failed: {exc}"

    version_str: str | None = getattr(pydantic_ai, "__version__", None)
    if version_str is None:
        return False, None, "pydantic_ai has no __version__ attribute"

    parsed = _parse_version_tuple(version_str)
    if parsed < _MIN_PYDANTIC_AI_VERSION:
        return (
            False,
            version_str,
            (
                f"pydantic_ai {version_str} < required "
                f"{'.'.join(str(p) for p in _MIN_PYDANTIC_AI_VERSION)}"
            ),
        )
    return True, version_str, None


def configure_logfire(
    *,
    service_name: str = "pyrene",
    send_to_logfire: Literal[
        "if-token-present", "always", "never"
    ] = "if-token-present",
    additional_span_processors: Sequence[SpanProcessor] | None = None,
    instrument_pydantic_ai: bool = True,
) -> InstrumentationStatus:
    """Idempotent Logfire configuration + Pydantic AI / SQLAlchemy hooks.

    Args:
        service_name: Resource attribute attached to every span. Recommended
            value per service: ``"pyrene-sql"``, ``"pyrene-eval"``.
        send_to_logfire: Controls the Logfire network sink.

            - ``"if-token-present"`` (default): export only when a
              ``LOGFIRE_TOKEN`` is set in the environment. Ideal for local
              dev + CI without a token.
            - ``"always"``: require token, raise on missing.
            - ``"never"``: emit spans into local processors only — used by
              the unit-test InMemorySpanExporter fixture.
        additional_span_processors: Extra OTel `SpanProcessor`s plumbed in
            via `logfire.configure(additional_span_processors=...)`. The
            test fixture uses this to attach an InMemorySpanExporter.
        instrument_pydantic_ai: If False, skip the Pydantic AI hook. Used
            by the fallback unit test that pretends the integration is
            unavailable on the installed Logfire build.

    Returns:
        The same `InstrumentationStatus` that `get_instrumentation_status`
        returns, populated with the outcome of this call.
    """
    global _STATUS

    # Late import so this module costs ~zero in environments that never
    # call `configure_logfire` (e.g. cheap import-time consumers).
    import logfire

    status = InstrumentationStatus(
        send_mode=send_to_logfire,
        additional_processors=(
            len(additional_span_processors)
            if additional_span_processors is not None
            else 0
        ),
    )

    # Configure Logfire itself. The signature accepts a Literal but the
    # `Literal["always"]` argument needs to map to ``True``; "never" maps
    # to ``False``; "if-token-present" passes through as the literal.
    logfire_send: bool | Literal["if-token-present"]
    if send_to_logfire == "always":
        logfire_send = True
    elif send_to_logfire == "never":
        logfire_send = False
    else:
        logfire_send = "if-token-present"

    logfire.configure(
        service_name=service_name,
        send_to_logfire=logfire_send,
        additional_span_processors=(
            list(additional_span_processors)
            if additional_span_processors is not None
            else None
        ),
        # Console output adds noise in tests; default opts in via
        # `LOGFIRE_CONSOLE` env var so we leave it `None` here.
        console=None,
    )
    status.configured = True

    # ADR-002 D3 — Pydantic AI instrumentation with version pin + fallback.
    if instrument_pydantic_ai:
        ok, version, reason = _check_pydantic_ai_version()
        status.pydantic_ai_version = version

        if not ok:
            status.fallback_reason = reason
            _install_httpx_fallback(status, reason or "version-check failed")
        else:
            try:
                # `instrument_pydantic_ai()` may not exist on older Logfire
                # builds (AttributeError) or may raise at call time
                # (RuntimeError, ImportError of an optional sub-dep).
                hook = getattr(logfire, "instrument_pydantic_ai", None)
                if hook is None:
                    raise AttributeError(
                        "logfire.instrument_pydantic_ai not available"
                    )
                hook()
                status.pydantic_ai_instrumented = True
            except (ImportError, AttributeError, RuntimeError) as exc:
                # Soft-fail: log + fall back to httpx instrumentation so we
                # still get model API call spans.
                reason_msg = f"instrument_pydantic_ai failed: {exc}"
                status.fallback_reason = reason_msg
                logger.warning(
                    "Pydantic AI instrumentation unavailable, falling back "
                    "to httpx instrumentation: %s",
                    exc,
                )
                _install_httpx_fallback(status, reason_msg)

    _STATUS = status
    return status


def _install_httpx_fallback(status: InstrumentationStatus, reason: str) -> None:
    """ADR-002 D3 fallback: instrument the httpx layer so model calls are traced."""
    import logfire

    try:
        instrument_httpx = getattr(logfire, "instrument_httpx", None)
        if instrument_httpx is None:
            warnings.warn(
                "logfire.instrument_httpx unavailable; trace richness reduced "
                f"(reason: {reason})",
                stacklevel=3,
            )
            status.extra_warnings = (
                *status.extra_warnings,
                f"httpx-fallback-unavailable: {reason}",
            )
            return
        instrument_httpx()
        status.httpx_fallback = True
    except Exception as exc:  # pragma: no cover - exotic env
        logger.warning("httpx fallback instrumentation failed: %s", exc)
        status.extra_warnings = (
            *status.extra_warnings,
            f"httpx-fallback-failed: {exc}",
        )


def instrument_engine(engine: AsyncEngine | Engine) -> None:
    """Wire SQLAlchemy 2.x async/sync engine into Logfire / OTel.

    SQLAlchemy 2.x async hook trap (PRD-006 §3 / PLAN-006 D3.7):

      `event.listen(AsyncEngine, "before_execute", listener)` is silently
      *skipped* — `AsyncEngine` is a thin wrapper, the real dispatch fires
      on `AsyncEngine.sync_engine`. So:

        do this:    event.listen(engine.sync_engine, "before_execute", fn)
        not this:   event.listen(engine, "before_execute", fn)        # silent no-op

    `logfire.instrument_sqlalchemy()` accepts either an `AsyncEngine` or a
    sync `Engine` directly; internally it does the `.sync_engine` unwrap so
    we don't have to. We use the high-level helper here so the unwrap is
    centralized in one place — and we cover the unwrap behavior with a
    unit test (`test_instrument_engine_async_uses_sync_engine`).
    """
    import logfire

    # If logfire.instrument_sqlalchemy is unavailable on this build, log a
    # warning and skip — DB spans will still come through any application
    # code that wraps `session.execute(...)` in a `logfire.span(...)`.
    hook = getattr(logfire, "instrument_sqlalchemy", None)
    if hook is None:
        logger.warning(
            "logfire.instrument_sqlalchemy unavailable; DB-level spans "
            "will not be auto-emitted."
        )
        return

    hook(engine=engine)
    _STATUS.sqlalchemy_engines += 1


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
