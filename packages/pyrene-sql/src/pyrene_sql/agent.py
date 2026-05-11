"""Pydantic AI `sql_analyst` agent + `AnalystResponse` output model.

PLAN-001 Day 2 + PLAN-002 Day 2 + PLAN-003 Day 1. Wires a single read-only
`run_select` tool to a Pydantic AI `Agent` that always emits the
`AnalystResponse` schema (PRD-001 §4.2) and exposes `run_with_retry`
(PRD-003) as the public entry point with the external retry wrapper applied.

Notes on Pydantic AI 1.93 patterns used here (cf. ADR-002):
- `output_type=AnalystResponse` makes the agent pin its terminal output to
  `AnalystResponse`. Pydantic AI synthesizes a `final_result` tool from this.
- `@sql_analyst.tool(retries=0)` disables native per-tool retry (D1). The
  external `RetryWrapper` (`pyrene_sql.retry`) owns the attempt counter.
- Dynamic system prompt (D2 — `@sql_analyst.system_prompt(dynamic=True)`)
  injects the schema-RAG top-3 context built from `Deps.schema_retriever`.
  The base SYSTEM_PROMPT below stays static; the dynamic part appends a
  "Relevant tables" section per run, gated by a token cap (PRD-002 §6).
"""

from __future__ import annotations

import os
from typing import Any

import logfire
from pydantic import Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UnexpectedModelBehavior

from pyrene_core import (
    SPAN_AGENT_RUN,
    SPAN_SQL_RUN_AGGREGATE,
    SPAN_SQL_RUN_JOIN,
    SPAN_SQL_RUN_SELECT,
    Confidence,
    ModelToolValidationError,
    PyreneError,
    SqlSyntaxError,
    StrictBaseModel,
)
from pyrene_sql.deps import Deps
from pyrene_sql.retry import (
    AttemptTrace,
    RetryDecision,
    RetryResult,
    RetryWrapper,
)
from pyrene_sql.schema.models import DEFAULT_CONNECTION_ID, SchemaChunk
from pyrene_sql.tools.execution import execute_run_select
from pyrene_sql.tools.models import RunAggregateInput, RunJoinInput
from pyrene_sql.tools.run_aggregate import execute_run_aggregate
from pyrene_sql.tools.run_join import execute_run_join
from pyrene_sql.tools.run_select import RunSelectInput, RunSelectOutput

# We read the model name directly from env so this module imports cheaply in
# unit tests (where Settings()'s required PG_DSN may be absent). Production
# callers that load Settings get the same value via the same env var.
_MODEL_NAME = os.getenv("MODEL_NAME", "anthropic:claude-sonnet-4-6")


class AnalystResponse(StrictBaseModel):
    """Terminal output of `sql_analyst`. PRD-001 §4.2 + PRD-003 §4."""

    sql: str | None = None
    rows: list[dict[str, Any]] | None = None
    row_count: int | None = None
    truncated: bool = False
    analysis: str = Field(
        default="",
        description="Natural-language explanation, including any assumptions made.",
    )
    confidence: Confidence
    refusal: str | None = None
    # Tuple (not list) keeps StrictBaseModel's frozen contract intact while
    # remaining JSON-serialisable. One entry per agent.run() attempt.
    attempts: tuple[AttemptTrace, ...] = ()


SYSTEM_PROMPT = """\
You are Pyrene's SQL analyst. You answer questions about a PostgreSQL
database by calling one of three structured tools: `run_select`, `run_join`,
or `run_aggregate`. You never write raw SQL — the tools are the only way you
read data.

Hard rules:
1. All three tools are READ-ONLY. The database role enforces this at the
   server side, but you must also refuse client-side. If the user asks to
   modify, delete, insert, drop, truncate, alter, or otherwise change data
   (DELETE, UPDATE, INSERT, DROP, TRUNCATE, ALTER, GRANT, REVOKE, CREATE,
   COPY ... FROM, etc.), do NOT call any tool. Return:
     - sql=None, rows=None, row_count=None, truncated=False
     - refusal: a short user-language sentence that explains the system is
       read-only and suggests a phrasing the user can try ("Try asking how
       many rows match instead of asking to delete them").
     - confidence=high (the refusal itself is unambiguous; PRD-001 L-03).

2. Tool selection (PRD-004 §2):
   - Use `run_select` for single-table queries with no aggregation.
   - Use `run_join` when the question requires combining two tables (a
     foreign-key join). Set `join.type="INNER"` for "X with their Y" framings
     and `join.type="LEFT"` for "not yet" / "never" / "missing" framings
     (the LEFT side keeps unmatched rows so `WHERE right.id IS NULL` returns
     the orphans).
   - Use `run_aggregate` when the question requires GROUP BY plus one of
     count / sum / avg / min / max. `run_aggregate` supports an optional
     single JOIN; do not chain more than that.
   - If the question requires three or more tables joined together, refuse
     with a short next-step suggestion (the tools do not support it in
     Phase 1 — PRD-004 §3.2).

3. When the user's intent is clear, call the chosen tool with structured
   arguments (tables as 'schema.table', columns as bare identifiers or '*',
   optional where with named params, limit ≤ 1000). After the tool returns,
   populate `AnalystResponse` with the SQL you intended (in human-readable
   form), the returned rows, row_count, truncated flag, a short `analysis`
   of the result, refusal=None, confidence=high.

4. When the question is ambiguous (e.g. "top movies" without a metric, or a
   table/column the user named cannot be located), make the smallest
   reasonable assumption, document it explicitly in `analysis`
   ("Assumption: 'top' = highest rental_rate"), and set confidence=medium.
   Do NOT invent table or column names you have not seen. If you cannot make
   any reasonable assumption, refuse with a helpful next-step suggestion and
   confidence=high.

5. Never expose internal error stack traces. If a tool call fails, summarize
   the failure in user language inside `refusal` or `analysis` and suggest
   the next action.

6. If a tool returns an error, read the error message and fix the call on
   the next attempt (e.g. wrong table/column name, missing JOIN). Do not
   repeat the exact failing call.

7. Always emit the `AnalystResponse` schema. Keep `analysis` concise (≤ 3
   sentences). Use the same language as the user's question.

Example (PRD-004 §2.1 S1 — top categories by revenue):
  Question: "지난 분기 매출이 가장 많은 영화 카테고리 5개"
  Call: run_aggregate(
    base_table="public.payment",
    joins=[{
      "table": "public.rental",
      "on": [("payment.rental_id", "rental.rental_id")],
      "type": "INNER",
    }],
    group_by=["rental.customer_id"],
    aggregations=[{"function": "sum", "column": "amount", "alias": "revenue"}],
    order_by=[{"column": "revenue", "direction": "desc"}],
    limit=5,
  )
  (Real Q1 chains payment → rental → inventory → film → film_category →
  category; since Phase 1 supports only one JOIN, fall back to a partial
  aggregation and explain the simplification in `analysis`.)
"""


sql_analyst: Agent[Deps, AnalystResponse] = Agent(
    model=_MODEL_NAME,
    output_type=AnalystResponse,
    deps_type=Deps,
    system_prompt=SYSTEM_PROMPT,
    # Defer provider/key validation so tests + import-time consumers without an
    # ANTHROPIC_API_KEY can still load this module. The wrapper / live test
    # path validates the model when `agent.run(...)` actually fires.
    defer_model_check=True,
)


# PRD-002 §6 caps the *schema* portion of the prompt at 2000 tokens (so the
# base prompt + top-3 chunks stay under the model's context budget). This is
# enforced inside the dynamic builder; if tiktoken is unavailable we fall back
# to a chars x 0.25 estimate (~4 chars/token for English+Korean mix, which is
# a deliberate over-count to err on the side of trimming earlier).
SCHEMA_PROMPT_TOKEN_CAP: int = 2000


def _estimate_tokens(text: str) -> int:
    """Token count estimate. Uses tiktoken when available, else chars/4.

    We pin the encoding to `cl100k_base` because it matches the OpenAI
    embedding model (`text-embedding-3-small`) that emits the vectors driving
    the retriever, and is a reasonable upper bound for Anthropic counts too —
    Anthropic tokenises Korean slightly more densely, but the 2000-token cap
    has enough headroom that we accept the approximation rather than pulling
    in a second tokenizer.
    """
    try:
        import tiktoken
    except ImportError:  # pragma: no cover - tiktoken is a hard dep via openai
        return max(1, len(text) // 4)
    try:
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:  # pragma: no cover - encoding file missing
        return max(1, len(text) // 4)
    return len(enc.encode(text))


def _format_schema_chunks(chunks: tuple[SchemaChunk, ...]) -> str:
    """Render top-k retrieved chunks as a fenced markdown block.

    The header is deliberately verbose ("Relevant tables ... use these and
    only these unless you have a strong reason ...") so the model treats the
    section as a constraint rather than a hint.
    """
    if not chunks:
        return ""

    header = (
        "Relevant tables (top schema-RAG matches for the current question). "
        "Prefer these tables; do not invent table or column names not listed "
        "here without first explaining your reasoning in `analysis`."
    )
    body = "\n\n".join(chunk.description for chunk in chunks)
    return f"{header}\n\n{body}"


def _trim_to_token_cap(text: str, cap: int) -> str:
    """Trim `text` so its estimated token count fits under `cap`.

    Strategy: estimate, and if over the cap, slice characters proportionally
    and re-estimate up to a small bounded number of passes. We intentionally
    cut at character boundaries (not token boundaries) — the model handles a
    mid-word truncation fine, and we leave the door open for tiktoken being
    absent in CI sandboxes.
    """
    estimated = _estimate_tokens(text)
    if estimated <= cap:
        return text

    # Proportional shrink + a small safety margin so re-estimate lands under.
    ratio = cap / estimated
    chars = max(1, int(len(text) * ratio * 0.95))
    trimmed = text[:chars]

    # One re-check is enough in practice; bail with a hard char-truncate if
    # the estimator still disagrees (defends against pathological inputs).
    if _estimate_tokens(trimmed) > cap:
        trimmed = trimmed[: max(1, cap * 3)]
    return trimmed + "\n\n[... schema context truncated to fit token budget ...]"


@sql_analyst.system_prompt(dynamic=True)
async def _schema_context(ctx: RunContext[Deps]) -> str:
    """Inject the top-k schema-RAG context for the current user prompt.

    Two no-op exits, both expected in normal operation:
      - `deps.schema_retriever is None`: unit tests (no DB), the `index-schema`
        CLI itself, or a pre-RAG smoke run. Returns "" — the static
        SYSTEM_PROMPT remains the sole instruction.
      - `ctx.prompt` is empty / non-string: a multi-modal turn or a tool-loop
        re-entry. We have nothing to embed, so we return "" rather than
        embedding an empty string (which would just retrieve top-3 by index
        order — confusing for the model).

    `dynamic=True` is the load-bearing flag: it tells Pydantic AI to
    re-evaluate this function on every `agent.run(...)` even when a
    `message_history` is provided, which is what makes per-turn schema
    retrieval correct in multi-turn conversations (ADR-002 D2).
    """
    retriever = ctx.deps.schema_retriever
    if retriever is None:
        return ""

    prompt = ctx.prompt
    if not isinstance(prompt, str) or not prompt.strip():
        return ""

    chunks = await retriever.top_k(
        prompt, k=3, connection_id=DEFAULT_CONNECTION_ID
    )
    if not chunks:
        # PRD-002 §2.2 F2 hint surfaces here too: an empty retriever result
        # (DB reachable but no rows) is operationally equivalent to "schema
        # not indexed". We do not raise — the agent can still attempt the
        # question, and the user-facing CLI prints a clearer warning when it
        # detects an empty `pyrene_schema_embeddings`.
        return ""

    rendered = _format_schema_chunks(chunks)
    return _trim_to_token_cap(rendered, SCHEMA_PROMPT_TOKEN_CAP)


def _summarize_input_for_sql(input: RunSelectInput) -> str:
    """Render the tool input as a human-readable SQL string for AttemptTrace.

    We don't have access to the actual interpolated SQL from `execute_run_select`
    without rerunning the renderer, but the structured input round-trips
    cleanly. The retry wrapper stamps this on RetryableError so the LLM /
    observability layer can see which call failed.
    """
    cols = (
        "*"
        if input.columns == "*"
        else ", ".join(input.columns)
    )
    where = f" WHERE {input.where}" if input.where else ""
    order = (
        " ORDER BY "
        + ", ".join(f"{s.column} {s.direction.upper()}" for s in input.order_by)
        if input.order_by
        else ""
    )
    return f"SELECT {cols} FROM {input.table}{where}{order} LIMIT {input.limit}"


def _stamp_user_team(span: logfire.LogfireSpan, ctx: RunContext[Deps]) -> None:
    """Stamp user_id / team_id from `Deps.user_context` onto an open span.

    Phase 1 is a no-op (UserContext is None) — but threading the helper
    now means PLAN-007 / PLAN-013 can fill these attributes by setting
    `Deps.user_context` without touching every tool.
    """
    uc = ctx.deps.user_context
    if uc is None:
        return
    span.set_attribute("user_id", str(uc.user_id))
    span.set_attribute("team_id", str(uc.team_id))


@sql_analyst.tool(retries=0)
async def run_select(
    ctx: RunContext[Deps], input: RunSelectInput
) -> RunSelectOutput:
    """Run a validated structured SELECT against the read-only DB role.

    Translates underlying SQLAlchemy / driver errors into the `PyreneError`
    hierarchy so the external `RetryWrapper` (PLAN-003) can classify them.
    Native per-tool retry is disabled (`retries=0`, ADR-002 D1).
    """
    with logfire.span(
        SPAN_SQL_RUN_SELECT,
        table=input.table,
        where=input.where,
        limit=input.limit,
    ) as span:
        _stamp_user_team(span, ctx)
        try:
            output = await execute_run_select(ctx.deps.db, input)
        except PyreneError:
            # Already classified upstream (or by a test stub). Pass through with
            # any sql attribute the caller stamped.
            raise
        except Exception as exc:
            # Phase 1 fallback: treat unknown DB-side errors as SQL syntax errors
            # (the retryable bucket). Phase 2 / PLAN-004 widens this with a
            # dedicated sqlalchemy.exc.* mapper.
            raise SqlSyntaxError(
                str(exc), sql=_summarize_input_for_sql(input)
            ) from exc
        span.set_attribute("row_count", output.row_count)
        span.set_attribute("truncated", output.truncated)
        return output


def _summarize_join_input(input: RunJoinInput) -> str:
    """Human-readable SQL summary for `RunJoinInput` (AttemptTrace)."""
    if input.select_left is None and input.select_right is None:
        cols = "*"
    else:
        parts: list[str] = []
        left_table = input.left.split(".", 1)[1]
        right_table = input.right.split(".", 1)[1]
        if input.select_left is None:
            parts.append(f"{left_table}.*")
        else:
            parts.extend(f"{left_table}.{c}" for c in input.select_left)
        if input.select_right is None:
            parts.append(f"{right_table}.*")
        else:
            parts.extend(f"{right_table}.{c}" for c in input.select_right)
        cols = ", ".join(parts)
    on_clause = " AND ".join(f"{lhs} = {rhs}" for lhs, rhs in input.join.on)
    where = f" WHERE {input.where}" if input.where else ""
    order = (
        " ORDER BY "
        + ", ".join(f"{s.column} {s.direction.upper()}" for s in input.order_by)
        if input.order_by
        else ""
    )
    return (
        f"SELECT {cols} FROM {input.left} {input.join.type} JOIN "
        f"{input.join.table} ON {on_clause}{where}{order} LIMIT {input.limit}"
    )


def _summarize_aggregate_input(input: RunAggregateInput) -> str:
    """Human-readable SQL summary for `RunAggregateInput` (AttemptTrace)."""
    group_by = ", ".join(input.group_by)
    aggs: list[str] = []
    for a in input.aggregations:
        expr = f"{a.function.upper()}({a.column})"
        if a.alias is not None:
            expr = f"{expr} AS {a.alias}"
        aggs.append(expr)
    agg_clause = ", ".join(aggs)
    join_clause = ""
    if input.joins:
        j = input.joins[0]
        on = " AND ".join(f"{lhs} = {rhs}" for lhs, rhs in j.on)
        join_clause = f" {j.type} JOIN {j.table} ON {on}"
    where = f" WHERE {input.where}" if input.where else ""
    order = (
        " ORDER BY "
        + ", ".join(f"{s.column} {s.direction.upper()}" for s in input.order_by)
        if input.order_by
        else ""
    )
    return (
        f"SELECT {group_by}, {agg_clause} FROM {input.base_table}"
        f"{join_clause}{where} GROUP BY {group_by}{order} LIMIT {input.limit}"
    )


@sql_analyst.tool(retries=0)
async def run_join(
    ctx: RunContext[Deps], input: RunJoinInput
) -> RunSelectOutput:
    """Run a validated 2-table JOIN against the read-only DB role.

    PRD-004 §2.1 S2. Same error-classification policy as `run_select`: DB-side
    errors become `SqlSyntaxError` so the retry wrapper can self-correct.
    """
    with logfire.span(
        SPAN_SQL_RUN_JOIN,
        left_table=input.left,
        right_table=input.right,
        join_type=input.join.type,
        where=input.where,
        limit=input.limit,
    ) as span:
        _stamp_user_team(span, ctx)
        try:
            output = await execute_run_join(ctx.deps.db, input)
        except PyreneError:
            raise
        except Exception as exc:
            raise SqlSyntaxError(
                str(exc), sql=_summarize_join_input(input)
            ) from exc
        span.set_attribute("row_count", output.row_count)
        span.set_attribute("truncated", output.truncated)
        return output


@sql_analyst.tool(retries=0)
async def run_aggregate(
    ctx: RunContext[Deps], input: RunAggregateInput
) -> RunSelectOutput:
    """Run a validated GROUP BY + aggregation query against the read-only DB.

    PRD-004 §2.1 S1. `aggregations` without `group_by` is rejected by the
    Pydantic model before this function runs (PRD-004 §6).
    """
    with logfire.span(
        SPAN_SQL_RUN_AGGREGATE,
        base_table=input.base_table,
        group_by=list(input.group_by),
        aggregations=[
            f"{a.function}({a.column})" for a in input.aggregations
        ],
        where=input.where,
        limit=input.limit,
    ) as span:
        _stamp_user_team(span, ctx)
        try:
            output = await execute_run_aggregate(ctx.deps.db, input)
        except PyreneError:
            raise
        except Exception as exc:
            raise SqlSyntaxError(
                str(exc), sql=_summarize_aggregate_input(input)
            ) from exc
        span.set_attribute("row_count", output.row_count)
        span.set_attribute("truncated", output.truncated)
        return output


def _build_refusal_response(
    error: PyreneError | None,
    decision: RetryDecision,
    attempts: tuple[AttemptTrace, ...],
) -> AnalystResponse:
    """Synthesize an AnalystResponse when the wrapper bails out.

    `abort_high_confidence_refusal` (permission denial) keeps confidence=high
    because the refusal IS the answer; everything else gets `low`.
    """
    confidence = (
        Confidence.high
        if decision is RetryDecision.abort_high_confidence_refusal
        else Confidence.low
    )
    refusal_msg = (
        str(error)
        if error is not None
        else "Maximum retry attempts exceeded without a successful query."
    )
    return AnalystResponse(
        sql=None,
        rows=None,
        row_count=None,
        truncated=False,
        analysis="",
        confidence=confidence,
        refusal=refusal_msg,
        attempts=attempts,
    )


async def run_with_retry(
    question: str,
    deps: Deps,
    *,
    max_attempts: int = 3,
    request_id: str | None = None,
) -> AnalystResponse:
    """Public entry point: run the analyst with the external retry wrapper.

    PRD-003 §4. The wrapper invokes `sql_analyst.run` up to `max_attempts`
    times. On the second+ attempt the previous error message is appended to
    the user prompt so the model can self-correct (the system prompt rule 5
    primes this behaviour). Returns `AnalystResponse` with `.attempts`
    populated. Hard cap stays at 3 (F-04 / L-02).

    `request_id` is stamped on the parent `pyrene.agent.run` span; pass a
    correlation id from the calling layer (CLI / API) so a trace can be
    cross-referenced with logs (PRD-006 §6, span attribute table).
    """
    uc = deps.user_context
    user_id = str(uc.user_id) if uc is not None else ""
    team_id = str(uc.team_id) if uc is not None else ""

    with logfire.span(
        SPAN_AGENT_RUN,
        question_length=len(question),
        max_attempts=max_attempts,
        model=_MODEL_NAME,
        user_id=user_id,
        team_id=team_id,
        request_id=request_id or "",
    ) as run_span:

        async def _one_attempt(
            attempt_idx: int, last_error: PyreneError | None, /
        ) -> AnalystResponse:
            prompt = question
            if attempt_idx > 1 and last_error is not None:
                prompt = (
                    f"{question}\n\n"
                    f"[Previous attempt {attempt_idx - 1} failed: {last_error}. "
                    "Please fix the SQL and try again.]"
                )
            # PRD-019 F-4: builder.py sets retries=0 on each tool, so an
            # LLM that emits malformed tool args raises UnexpectedModelBehavior
            # rather than something the wrapper can classify. Wrap into
            # ModelToolValidationError (RetryableError) so decide() applies
            # the standard N1-N4 policy.
            try:
                result = await sql_analyst.run(prompt, deps=deps)
            except UnexpectedModelBehavior as exc:
                raise ModelToolValidationError(str(exc)) from exc
            return result.output

        wrapper = RetryWrapper(max_attempts=max_attempts)
        outcome: RetryResult = await wrapper.run(_one_attempt)

        run_span.set_attribute("attempt_count", len(outcome.attempts))
        if outcome.value is not None:
            # Re-emit AnalystResponse with the collected attempts attached.
            # The wrapper's last AttemptTrace carries the successful SQL
            # (best effort).
            assert isinstance(outcome.value, AnalystResponse)
            run_span.set_attribute("outcome", "success")
            run_span.set_attribute(
                "confidence", outcome.value.confidence.value
            )
            return outcome.value.model_copy(
                update={"attempts": outcome.attempts}
            )

        decision = outcome.final_decision or RetryDecision.abort_low_confidence
        run_span.set_attribute("outcome", str(decision))
        return _build_refusal_response(
            outcome.final_error, decision, outcome.attempts
        )


__all__ = [
    "SCHEMA_PROMPT_TOKEN_CAP",
    "AnalystResponse",
    "run_aggregate",
    "run_join",
    "run_select",
    "run_with_retry",
    "sql_analyst",
]
