"""Pydantic request/response schemas for the RBAC matrix API.

Strict + frozen models keep request bodies immutable post-validation
and surface stray fields as 422 instead of silently dropping them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from pyrene_core import StrictBaseModel

# `Literal[...]` here is the canonical Pydantic v2 shape for the
# action column. The DB stores `String(8)`; the schema layer is what
# guards against arbitrary values landing in the table.
PermissionAction = Literal["allow", "deny"]


class PermissionCreateRequest(StrictBaseModel):
    """Body for POST /rbac/permissions.

    `tool_name` is exact-match (F-02); the resolver normalizes inputs
    (strip + lower) at lookup time, and the schema enforces the
    canonical form here so the matrix UI does not store rows that the
    resolver can never reach.
    """

    role_id: UUID
    tool_name: str = Field(min_length=1, max_length=128)
    action: PermissionAction = "allow"


class PermissionUpdateRequest(StrictBaseModel):
    """Body for PUT /rbac/permissions/{id} — only `action` is mutable.

    `role_id` / `tool_name` form the row's identity (unique together
    with action); flipping action is the only sensible mutation, and
    keeps the cache invalidation surface tiny.
    """

    action: PermissionAction


class PermissionResponse(StrictBaseModel):
    id: UUID
    role_id: UUID
    tool_name: str
    action: PermissionAction
    created_at: datetime


class MatrixRoleEntry(StrictBaseModel):
    """One role row in the `GET /rbac/matrix` response."""

    role_id: UUID
    role_name: str
    # Map tool_name -> action ("allow" | "deny"). Tools absent from
    # the map are deny-by-default (PRD-010 §2.2 F1).
    tools: dict[str, PermissionAction]


class MatrixResponse(StrictBaseModel):
    """Role x Tool 2D matrix snapshot (PRD-010 §4)."""

    roles: list[MatrixRoleEntry]
    tools: list[str]


__all__ = [
    "MatrixResponse",
    "MatrixRoleEntry",
    "PermissionAction",
    "PermissionCreateRequest",
    "PermissionResponse",
    "PermissionUpdateRequest",
]
