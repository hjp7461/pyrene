"""Pydantic schema validation — admin-grant tripwire + normalization.

PRD-011 §위험 #3 + PM amend: full wildcard allow rows require explicit
`is_admin_grant=True`. The schema layer is the first line of defense
because the resolver itself honors the row once it lands in the DB.
"""

from __future__ import annotations

import warnings
from uuid import uuid4

import pytest

from pyrene_data_rbac import (
    DataPermissionCreateRequest,
    DataPermissionUpdateRequest,
)


def test_create_request_accepts_explicit_table() -> None:
    body = DataPermissionCreateRequest(
        role_id=uuid4(),
        connection_id=uuid4(),
        schema="public",
        table="payment",
        action="allow",
    )
    assert body.schema == "public"
    assert body.table == "payment"
    assert body.action == "allow"


def test_create_request_normalizes_uppercase() -> None:
    """PM amend bypass case #3 — uppercase schema/table is lower-cased."""
    body = DataPermissionCreateRequest(
        role_id=uuid4(),
        connection_id=uuid4(),
        schema="PUBLIC",
        table="PAYMENT",
    )
    assert body.schema == "public"
    assert body.table == "payment"


def test_create_request_strips_quotes() -> None:
    """PM amend bypass cases #1 / #2 — quoted identifiers normalize."""
    body = DataPermissionCreateRequest(
        role_id=uuid4(),
        connection_id=uuid4(),
        schema='"public"',
        table='"payment"',
    )
    assert body.schema == "public"
    assert body.table == "payment"


def test_full_wildcard_allow_requires_admin_grant_flag() -> None:
    """PRD-011 §위험 #3 — (schema='*', table='*', allow) needs explicit ack."""
    with pytest.raises(ValueError, match="admin-equivalent"):
        DataPermissionCreateRequest(
            role_id=uuid4(),
            connection_id=uuid4(),
            schema="*",
            table="*",
            action="allow",
        )


def test_full_wildcard_allow_with_flag_emits_warning() -> None:
    """When the caller passes `is_admin_grant=True`, the schema accepts
    the row but emits a UserWarning so test logs and admin UIs can
    surface the elevated grant."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        body = DataPermissionCreateRequest(
            role_id=uuid4(),
            connection_id=uuid4(),
            schema="*",
            table="*",
            action="allow",
            is_admin_grant=True,
        )
    assert body.schema == "*"
    assert body.table == "*"
    assert any(
        "full wildcard admin grant" in str(w.message) for w in caught
    )


def test_full_wildcard_deny_does_not_require_admin_flag() -> None:
    """Wildcard deny is the OPPOSITE of admin — denies every table on
    the connection. No tripwire required (it strips privileges)."""
    body = DataPermissionCreateRequest(
        role_id=uuid4(),
        connection_id=uuid4(),
        schema="*",
        table="*",
        action="deny",
    )
    assert body.action == "deny"


def test_schema_wildcard_only_does_not_require_admin_flag() -> None:
    """Partial wildcard (schema='*', table='payment') only grants
    `payment` reads across every schema. Still scoped enough not to
    trip the admin guard."""
    body = DataPermissionCreateRequest(
        role_id=uuid4(),
        connection_id=uuid4(),
        schema="*",
        table="payment",
        action="allow",
    )
    assert body.schema == "*"
    assert body.table == "payment"


def test_update_request_only_carries_action() -> None:
    body = DataPermissionUpdateRequest(action="deny")
    assert body.action == "deny"


def test_create_request_rejects_invalid_action() -> None:
    with pytest.raises(ValueError):
        DataPermissionCreateRequest(
            role_id=uuid4(),
            connection_id=uuid4(),
            schema="public",
            table="payment",
            action="execute",  # type: ignore[arg-type]
        )


def test_create_request_rejects_extra_fields() -> None:
    """StrictBaseModel rejects stray keys → 422 in the route."""
    with pytest.raises(ValueError):
        DataPermissionCreateRequest(  # type: ignore[call-arg]
            role_id=uuid4(),
            connection_id=uuid4(),
            schema="public",
            table="payment",
            action="allow",
            extra="surprise",
        )
