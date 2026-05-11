"""Pydantic request/response schemas for the auth API.

Frozen StrictBaseModel keeps request bodies immutable post-validation and
prevents accidental field additions from leaking through.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import EmailStr, Field

from pyrene_core import StrictBaseModel


class SignupRequest(StrictBaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(StrictBaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(StrictBaseModel):
    refresh_token: str


class TokenPairResponse(StrictBaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AccessTokenResponse(StrictBaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(StrictBaseModel):
    id: UUID
    email: str
    team_id: UUID
    roles: tuple[str, ...]


class RoleResponse(StrictBaseModel):
    id: UUID
    name: str
    description: str


class RoleCreateRequest(StrictBaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)


class RoleUpdateRequest(StrictBaseModel):
    description: str = Field(default="", max_length=512)


__all__ = [
    "AccessTokenResponse",
    "LoginRequest",
    "RefreshRequest",
    "RoleCreateRequest",
    "RoleResponse",
    "RoleUpdateRequest",
    "SignupRequest",
    "TokenPairResponse",
    "UserResponse",
]
