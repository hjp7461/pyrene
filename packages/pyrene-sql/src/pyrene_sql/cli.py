"""Pyrene SQL CLI entry point.

Commands:
  - `pyrene-sql ask`           — natural-language ask (PLAN-001 Day 3)
  - `pyrene-sql index-schema`  — populate `pyrene_schema_embeddings` (PLAN-002 Day 1)
"""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import TYPE_CHECKING, Any
from uuid import UUID

import typer

from pyrene_sql.schema import DEFAULT_CONNECTION_ID, OpenAIEmbedder, SchemaIndexer

if TYPE_CHECKING:
    from pyrene_sql.agent import AnalystResponse

app = typer.Typer(help="Pyrene SQL analyst")


@app.callback()
def _main() -> None:
    """Pyrene SQL — natural language to safe PostgreSQL queries."""


@app.command()
def ask(
    question: str,
    pretty: bool = typer.Option(
        False,
        "--pretty",
        help="Render the response as a rich table instead of JSON.",
    ),
    trace: bool = typer.Option(
        False,
        "--trace",
        help=(
            "Emit the active OTel trace_id (and the Logfire UI URL when "
            "LOGFIRE_TOKEN is set) so the run can be cross-referenced."
        ),
    ),
) -> None:
    """Ask a natural-language question against the configured PostgreSQL DB.

    Phase 1 flow (PRD-001 §2):
      1. Load Settings (PG_READONLY_DSN, MODEL_NAME, ANTHROPIC_API_KEY).
      2. Create a read-only async engine + session.
      3. Build `Deps` with `db=session`, `user_context=None`.
         The optional `schema_retriever` field (PRD-002 Day 2) is set to
         `None` here — Phase 1 CLI runs without RAG context; the agent's
         dynamic system prompt short-circuits to an empty string when the
         retriever is None, so the base SYSTEM_PROMPT still drives behavior.
      4. Invoke `run_with_retry` (PRD-003) — wraps `sql_analyst.run` with the
         external attempt loop (≤ 3 attempts, F-04).
      5. Print JSON (default) or rich Table (--pretty).
    """
    response, trace_id = asyncio.run(_run_ask(question))
    if pretty:
        _render_pretty(response)
    else:
        typer.echo(response.model_dump_json(indent=2))
    if trace:
        if trace_id is not None:
            typer.echo(f"trace_id: {trace_id}", err=True)
            if os.getenv("LOGFIRE_TOKEN"):
                typer.echo(
                    f"logfire: https://logfire-us.pydantic.dev/-/redirect/"
                    f"latest-traces?trace_id={trace_id}",
                    err=True,
                )
        else:
            typer.echo("trace_id: <none — Logfire not configured>", err=True)


async def _run_ask(question: str) -> tuple[AnalystResponse, str | None]:
    """Wire Settings → engine → session → Deps → agent.

    Settings are loaded lazily (here, not at module import) so that
    `pyrene-sql --help` and `pyrene-sql index-schema --help` work even when
    PG_READONLY_DSN / ANTHROPIC_API_KEY are unset.

    Returns the response paired with the OTel trace id (hex) so the caller
    can echo a Logfire UI link under `--trace`. The trace id is captured
    from the active span *inside* the agent.run wrapper.
    """
    from pyrene_core import configure_logfire, instrument_engine
    from pyrene_sql.agent import run_with_retry
    from pyrene_sql.db import make_readonly_engine, make_session_factory
    from pyrene_sql.deps import Deps
    from pyrene_sql.settings import Settings

    settings = Settings()  # type: ignore[call-arg]

    # PRD-019 F-3: Pydantic AI providers read os.environ directly; pydantic-
    # settings populates the Settings object but does not export to environ.
    # setdefault preserves shell-exported values for advanced users while
    # making .env-only setup work for fresh installs.
    if settings.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)

    # PRD-006 §2.2 F1: missing LOGFIRE_TOKEN must not block the agent.
    # `if-token-present` opts in only when a token is set; spans still flow
    # through any locally-attached processors, and Pydantic AI / SQLAlchemy
    # instrumentation runs unconditionally.
    configure_logfire(
        service_name=settings.logfire_project_name,
        send_to_logfire="if-token-present",
    )
    engine = make_readonly_engine(settings)
    instrument_engine(engine)
    session_factory = make_session_factory(engine)

    # Defensive construction: `Deps` was extended in PLAN-002 Day 2 with the
    # optional `schema_retriever` field. If a parallel sub-agent has not yet
    # landed that change, fall back to the smaller signature so this CLI keeps
    # working through the rollout window.
    deps_params = inspect.signature(Deps).parameters
    deps_kwargs: dict[str, Any] = {"db": None, "user_context": None}
    if "schema_retriever" in deps_params:
        deps_kwargs["schema_retriever"] = None

    try:
        async with session_factory() as session:
            deps_kwargs["db"] = session
            deps = Deps(**deps_kwargs)
            # Open a CLI-level parent span so we have a stable trace_id to
            # echo back even after `run_with_retry` exits its own span.
            import logfire

            with logfire.span("pyrene.cli.ask") as cli_span:
                response = await run_with_retry(question, deps)
                trace_id = _trace_id_from_span(cli_span)
            return response, trace_id
    finally:
        await engine.dispose()


def _trace_id_from_span(span: Any) -> str | None:
    """Return the trace id (32-char hex) attached to `span`, or None.

    Logfire's `LogfireSpan` exposes `.context` (the OTel `SpanContext`)
    while the span is open. If logfire is unconfigured the NoOp tracer
    yields a span with an invalid context — we map that to None.
    """
    try:
        from opentelemetry.trace import format_trace_id
    except ImportError:  # pragma: no cover - opentelemetry is a hard dep
        return None
    ctx = getattr(span, "context", None)
    if ctx is None or not getattr(ctx, "is_valid", False):
        return None
    return format_trace_id(ctx.trace_id)


def _render_pretty(response: AnalystResponse) -> None:
    """Render an AnalystResponse as a rich layout.

    `rich` is a hard dependency of pyrene-sql, but we still import it inside
    the function so that callers that never use `--pretty` are not forced to
    import rich on every CLI invocation.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()

    # Header: confidence + refusal/sql summary
    confidence = response.confidence.value
    confidence_color = {
        "high": "green",
        "medium": "yellow",
        "low": "red",
    }.get(confidence, "white")

    summary = Table.grid(padding=(0, 1))
    summary.add_column(style="bold cyan", no_wrap=True)
    summary.add_column()
    summary.add_row("confidence", f"[{confidence_color}]{confidence}[/]")
    summary.add_row("attempts", str(len(response.attempts)))
    if response.refusal:
        summary.add_row("refusal", response.refusal)
    if response.sql:
        summary.add_row("sql", response.sql)
    if response.row_count is not None:
        summary.add_row(
            "row_count",
            f"{response.row_count}{' (truncated)' if response.truncated else ''}",
        )
    console.print(Panel(summary, title="Pyrene SQL — answer", border_style="cyan"))

    # Rows preview (top 5)
    if response.rows:
        preview = response.rows[:5]
        columns = list(preview[0].keys()) if preview else []
        rows_table = Table(
            title=f"rows (showing {len(preview)} of {response.row_count or len(response.rows)})",
            show_lines=False,
        )
        for col in columns:
            rows_table.add_column(col, overflow="fold")
        for row in preview:
            rows_table.add_row(*[str(row.get(col, "")) for col in columns])
        console.print(rows_table)

    if response.analysis:
        console.print(Panel(response.analysis, title="analysis", border_style="magenta"))


@app.command("index-schema")
def index_schema(
    reindex: bool = typer.Option(
        False,
        "--reindex",
        help="Delete all rows for this connection before re-inserting.",
    ),
    connection_id: str = typer.Option(
        str(DEFAULT_CONNECTION_ID),
        "--connection-id",
        help="UUID of the connection to scope this index under (Phase 1: default).",
    ),
) -> None:
    """Index the PostgreSQL schema into `pyrene_schema_embeddings` (PRD-002 §2.1 S1)."""

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        typer.echo(
            "OPENAI_API_KEY is not set. Export it (or add it to .env) before running "
            "`index-schema`; the default embedder is OpenAI text-embedding-3-small.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        cid = UUID(connection_id)
    except ValueError as exc:
        typer.echo(f"Invalid --connection-id (must be a UUID): {exc}", err=True)
        raise typer.Exit(code=2) from None

    count = asyncio.run(_run_index_schema(api_key=api_key, connection_id=cid, reindex=reindex))
    typer.echo(f"Indexed {count} table chunk(s) into pyrene_schema_embeddings.")


async def _run_index_schema(*, api_key: str, connection_id: UUID, reindex: bool) -> int:
    """Wire up settings → engine → session → indexer → run.

    Settings are loaded here (not at module import) so `pyrene-sql --help`
    works even when PG_DSN is unset.
    """
    from pyrene_sql.db import make_session_factory, make_write_engine
    from pyrene_sql.settings import Settings

    settings = Settings()  # type: ignore[call-arg]
    engine = make_write_engine(settings)
    session_factory = make_session_factory(engine)

    try:
        async with session_factory() as session:
            embedder = OpenAIEmbedder(api_key=api_key)
            indexer = SchemaIndexer(
                write_session=session,
                embedder=embedder,
                connection_id=connection_id,
            )
            return await indexer.index_all(reindex=reindex)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    app()
