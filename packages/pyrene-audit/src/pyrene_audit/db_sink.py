"""`DBAuditSink` — `AuditSink` implementation backed by `audit_events`.

PLAN-015 Day 1. Satisfies `pyrene_core.audit.AuditSink` Protocol via
structural typing so the gateway slot-swap (`gateway.audit_sink =
DBAuditSink(...)`) is a one-liner at app startup.

Contract notes:
  - Application code MUST NOT set `prev_hash` / `row_hash`. The BEFORE
    INSERT trigger (`audit_set_row_hash`) reads the chain tip per
    `team_id` and stamps both columns. INSERT statements omit them
    entirely.
  - Duplicate `id` (caller-generated `uuid4`) collisions degrade
    gracefully — the sink swallows `IntegrityError` (event was already
    persisted; idempotency at the gateway hook is acceptable).
  - Every other exception propagates. PRD-015 F1 (fail-closed) makes
    audit write failure a request-blocking error; the gateway hook
    re-raises out of `Gateway.run(...)` for the route layer to map.

Session ownership:
  The sink takes an `async_sessionmaker` rather than a single
  `AsyncSession` so each `emit(...)` runs in its own short transaction
  — long-running tool invocations don't accidentally bundle audit
  writes into the surrounding savepoint and lose them on rollback.
"""

from __future__ import annotations

import logging

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyrene_audit.models import AuditEventRow
from pyrene_core.audit import AuditEvent

logger = logging.getLogger(__name__)


class DBAuditSink:
    """Persist `AuditEvent` rows into Postgres `audit_events`.

    Structurally implements `pyrene_core.audit.AuditSink` (the Protocol is
    `@runtime_checkable` — `isinstance(DBAuditSink(...), AuditSink)` returns
    `True`).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def emit(self, event: AuditEvent) -> None:
        """Insert one `audit_events` row.

        The BEFORE INSERT trigger populates `prev_hash` + `row_hash`;
        the client never supplies them.
        """
        try:
            async with self._session_factory() as session:
                # Use the Python attribute name `event_metadata` (the
                # column itself is `metadata` on the SQL side; the ORM
                # alias dodges SQLAlchemy's reserved `MetaData` slot).
                await session.execute(
                    insert(AuditEventRow).values(
                        id=event.id,
                        event_type=event.event_type,
                        user_id=event.user_id,
                        team_id=event.team_id,
                        agent_id=event.agent_id,
                        request_id=event.request_id,
                        tool_name=event.tool_name,
                        outcome=event.outcome,
                        event_metadata=event.metadata,
                        created_at=event.created_at,
                    )
                )
                await session.commit()
        except IntegrityError as exc:
            # Duplicate id — idempotent re-emit. Log + swallow.
            logger.warning(
                "audit duplicate id=%s event_type=%s (graceful skip)",
                event.id,
                event.event_type,
                exc_info=exc,
            )


__all__ = ["DBAuditSink"]
