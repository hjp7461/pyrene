"""Pydantic request/response schemas for the data-RBAC matrix API.

Strict + frozen models keep request bodies immutable post-validation
and surface stray fields as 422 instead of silently dropping them.

The wildcard `*` is allowed in `schema` and `table`; both being `*`
yields an admin-equivalent grant on a given connection. PRD-011 §위험 #3
and the PM amend mandate that this combination requires the explicit
`is_admin_grant=True` flag so an admin cannot create the row by
accident.
"""

from __future__ import annotations

import warnings
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from pyrene_core import StrictBaseModel

# Suppress the harmless Pydantic warning that `schema` shadows a
# deprecated BaseModel attribute. PRD-011 §4 specifies `schema` as the
# exact field name on the wire so we keep the shadow rather than
# renaming. Same pattern used by `pyrene_sql.schema.models`.
_SCHEMA_SHADOW_MSG = (
    r'Field name "schema" in "DataPermission\w+" '
    r'shadows an attribute in parent "StrictBaseModel"'
)
warnings.filterwarnings("ignore", message=_SCHEMA_SHADOW_MSG)

# `Literal[...]` here is the canonical Pydantic v2 shape for the
# action column. The DB stores `String(8)`; the schema layer is what
# guards against arbitrary values landing in the table.
DataPermissionAction = Literal["allow", "deny"]

# Wildcard sentinel — every schema, every table. Spelt out so the
# resolver / hook can compare without magic-string repetition.
WILDCARD: Literal["*"] = "*"


def _normalize_identifier(value: str) -> str:
    """Lower-case + strip surrounding whitespace.

    Schema-qualified identifiers in this package are stored in their
    canonical lowercase form. The resolver normalizes lookups with
    the same transform so `PUBLIC.payment` and `public.payment`
    collide on the same matrix row. The hook layer also strips quotes
    (`"public"."payment"`) before invoking the resolver — see
    `hooks.parse_qualified` for the full bypass-attempt surface
    (PM amend, schema-qualified bypass 5 cases).
    """
    return value.strip().strip('"').lower()


class DataPermissionCreateRequest(StrictBaseModel):
    """Body for POST /rbac/data-permissions.

    `is_admin_grant` is a tripwire — the resolver applies wildcard
    matching automatically, but creating the row `(schema="*",
    table="*")` is effectively an admin grant for the connection.
    Forcing the caller to pass `True` ensures the warning surfaces
    in code review and audit log.
    """

    role_id: UUID
    connection_id: UUID
    # `schema` shadows BaseModel.schema (deprecated). PRD-011 §4 spells
    # the field as `schema` on the wire so we keep the shadowing and
    # silence the mypy "assignment" complaint. Same pattern used by
    # `pyrene_sql.schema.models.SchemaChunk`.
    schema: str = Field(min_length=1, max_length=128)  # type: ignore[assignment]
    table: str = Field(min_length=1, max_length=512)
    action: DataPermissionAction = "allow"
    is_admin_grant: bool = False

    @model_validator(mode="after")
    def _normalize_and_check_wildcards(self) -> DataPermissionCreateRequest:
        # Normalize lowercase / strip so the matrix carries the canonical
        # form. Pydantic frozen models forbid attribute assignment, so we
        # rebuild via `model_copy(update=...)` — but since this validator
        # runs at construction it sees a mutable instance.
        object.__setattr__(self, "schema", _normalize_identifier(self.schema))
        object.__setattr__(self, "table", _normalize_identifier(self.table))

        is_full_wildcard = self.schema == WILDCARD and self.table == WILDCARD
        if is_full_wildcard and self.action == "allow" and not self.is_admin_grant:
            # PRD-011 §위험 #3 + PM amend: full wildcard allow grants
            # admin-equivalent read on the connection. Refuse the
            # row unless the caller acknowledged via `is_admin_grant`.
            raise ValueError(
                "creating an (schema='*', table='*', action='allow') row is "
                "admin-equivalent for this connection; pass is_admin_grant=True "
                "to confirm (PRD-011 §위험 #3)"
            )
        if is_full_wildcard and self.action == "allow" and self.is_admin_grant:
            # Surface as a warning so test logs and admin UIs can show
            # the elevated grant. The resolver still honours the row.
            warnings.warn(
                f"data-RBAC: full wildcard admin grant created (role={self.role_id}, "
                f"connection={self.connection_id}). PRD-011 §위험 #3 — review the role membership.",
                UserWarning,
                stacklevel=2,
            )
        return self


class DataPermissionUpdateRequest(StrictBaseModel):
    """Body for PUT /rbac/data-permissions/{id} — only `action` is mutable.

    `(role_id, connection_id, schema, table)` form the row identity (unique
    together with action); flipping action is the only sensible mutation
    and keeps the cache invalidation surface tiny.
    """

    action: DataPermissionAction


class DataPermissionResponse(StrictBaseModel):
    id: UUID
    role_id: UUID
    connection_id: UUID
    schema: str  # type: ignore[assignment]
    table: str
    action: DataPermissionAction
    created_at: datetime


__all__ = [
    "WILDCARD",
    "DataPermissionAction",
    "DataPermissionCreateRequest",
    "DataPermissionResponse",
    "DataPermissionUpdateRequest",
]
