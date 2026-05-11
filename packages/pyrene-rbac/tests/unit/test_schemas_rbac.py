"""Pydantic schema validation for the RBAC request/response shapes."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from pyrene_rbac.schemas import (
    MatrixResponse,
    MatrixRoleEntry,
    PermissionCreateRequest,
    PermissionUpdateRequest,
)


def test_create_request_defaults_to_allow() -> None:
    body = PermissionCreateRequest(role_id=uuid4(), tool_name="run_select")
    assert body.action == "allow"


def test_create_request_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        PermissionCreateRequest(
            role_id=uuid4(),
            tool_name="run_select",
            action="maybe",  # type: ignore[arg-type]
        )


def test_create_request_rejects_empty_tool_name() -> None:
    with pytest.raises(ValidationError):
        PermissionCreateRequest(role_id=uuid4(), tool_name="")


def test_create_request_rejects_extra_fields() -> None:
    """StrictBaseModel forbids extra keys — surfaces typos as 422."""
    with pytest.raises(ValidationError):
        PermissionCreateRequest.model_validate(
            {
                "role_id": str(uuid4()),
                "tool_name": "run_select",
                "extra": "nope",
            }
        )


def test_update_request_action_required() -> None:
    with pytest.raises(ValidationError):
        PermissionUpdateRequest.model_validate({})


def test_matrix_response_empty() -> None:
    response = MatrixResponse(roles=[], tools=[])
    assert response.roles == []
    assert response.tools == []


def test_matrix_role_entry_round_trip() -> None:
    rid = uuid4()
    entry = MatrixRoleEntry(
        role_id=rid, role_name="viewer", tools={"run_select": "allow"}
    )
    assert entry.role_id == rid
    assert entry.tools == {"run_select": "allow"}
