"""User identity carried through tool calls.

Phase 1: instances are not produced — `Deps.user_context` is `None`.
Phase 2 (PRD-007): the auth middleware injects this for every authenticated request.

Defining it now (BRIEF §6.2-5: "나중에 RBAC 붙이면 됨"을 핑계로 미루지 않는다)
gives Phase 1 tools a typed seat for Phase 2 to fill, without a later refactor.
"""

from __future__ import annotations

from uuid import UUID

from pyrene_core.models import StrictBaseModel


class UserContext(StrictBaseModel):
    """Identity + team scope + role names. PRD-001 §4.3, PRD-007 §4."""

    user_id: UUID
    team_id: UUID
    roles: tuple[str, ...]
