"""Dependency injection container for tool calls.

PRD-001 §4.3 + PRD-002 §5. `Deps` is created per-request and passed to every
tool (and to the dynamic system_prompt) via Pydantic AI's `RunContext[Deps]`.
Phase 1 carries the DB session, an optional `UserContext`, and an optional
`SchemaRetriever`; Phase 2 fills the user_context and adds the
audit/cost/budget sinks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_core import UserContext

if TYPE_CHECKING:
    # Import-time cycle guard: `schema.retriever` imports nothing from this
    # module, but pulling it eagerly here would make `from pyrene_sql.deps
    # import Deps` drag the pgvector path into every consumer (e.g. the CLI
    # `ask` skeleton that does not need it).
    from pyrene_sql.schema.retriever import SchemaRetriever


@dataclass(frozen=True, slots=True)
class Deps:
    """Per-request dependencies passed into Pydantic AI tools.

    Phase 2 will add:
      - audit_sink: AuditSink
      - cost_meter: CostMeter
      - budget_guard: BudgetGuard
    """

    db: AsyncSession
    user_context: UserContext | None = None
    # `None` is a first-class value: it disables the dynamic schema prompt so
    # unit tests (which never index) and the indexing CLI itself can construct
    # a `Deps` without needing a real retriever. PRD-002 §2.2 F2 ("schema not
    # indexed") is also conveyed through this — see `agent._schema_context`.
    schema_retriever: SchemaRetriever | None = None
