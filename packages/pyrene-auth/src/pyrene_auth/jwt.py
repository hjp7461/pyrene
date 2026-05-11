"""JWT issuance and verification (PRD-007 §4, ADR-006).

Self-contained PyJWT wrapper. HS256 only (single-instance assumption, BRIEF
§10). The `TokenPayload` is a frozen `StrictBaseModel` so the issuance /
verification path doesn't drift from `UserContext` (PRD-007 §4):

    sub:      user_id (UUID)
    team_id:  active team (UUID | None on refresh tokens)
    roles:    tuple of role names for the active team
    exp / iat: UTC seconds since epoch
    type:     "access" | "refresh"

Refresh tokens carry only `sub` + `type=refresh` so a leaked refresh token
cannot impersonate role grants directly — the refresh endpoint must re-read
the database to issue a new access token with current roles.
"""

from __future__ import annotations

import time
from typing import Any, Literal, cast
from uuid import UUID

import jwt as pyjwt
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pyrene_core import StrictBaseModel


class JwtSettings(BaseSettings):
    """JWT runtime configuration. Read from env / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="JWT_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    secret: str = Field(
        default="pyrene-dev-secret-change-me",
        description="HMAC secret. Override via JWT_SECRET in production.",
    )
    algorithm: Literal["HS256"] = Field(
        default="HS256",
        description="Only HS256 supported in Phase 2 (single-service deployment).",
    )
    access_ttl_seconds: int = Field(
        default=900,  # 15 minutes
        description="Access token lifetime.",
    )
    refresh_ttl_seconds: int = Field(
        default=604800,  # 7 days
        description="Refresh token lifetime.",
    )


class TokenPayload(StrictBaseModel):
    """Decoded JWT payload.

    Frozen StrictBaseModel — once decoded, attributes are immutable. Mirrors
    `UserContext` (PRD-007 §4) for access tokens; refresh tokens leave
    `team_id=None` and `roles=()` (re-read from DB on refresh).
    """

    sub: UUID
    team_id: UUID | None
    roles: tuple[str, ...]
    exp: int
    iat: int
    type: Literal["access", "refresh"]


class InvalidTokenError(Exception):
    """Token signature is bad, expired, or has wrong claims."""


def encode_token(payload: TokenPayload, settings: JwtSettings) -> str:
    """Serialise a TokenPayload into a signed JWT string."""
    data: dict[str, Any] = {
        "sub": str(payload.sub),
        "team_id": str(payload.team_id) if payload.team_id is not None else None,
        "roles": list(payload.roles),
        "exp": payload.exp,
        "iat": payload.iat,
        "type": payload.type,
    }
    return pyjwt.encode(data, settings.secret, algorithm=settings.algorithm)


def decode_token(token: str, settings: JwtSettings) -> TokenPayload:
    """Verify signature + expiration, parse into a TokenPayload.

    Raises `InvalidTokenError` on any failure (signature, expiration,
    malformed claims). Callers must translate to HTTP 401.
    """
    try:
        raw = pyjwt.decode(token, settings.secret, algorithms=[settings.algorithm])
    except pyjwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("token expired") from exc
    except pyjwt.InvalidTokenError as exc:
        raise InvalidTokenError(f"invalid token: {exc}") from exc

    try:
        return TokenPayload(
            sub=UUID(cast(str, raw["sub"])),
            team_id=UUID(raw["team_id"]) if raw.get("team_id") else None,
            roles=tuple(raw.get("roles", [])),
            exp=int(raw["exp"]),
            iat=int(raw["iat"]),
            type=raw["type"],
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError(f"malformed claims: {exc}") from exc


def make_access_token(
    user_id: UUID,
    team_id: UUID | None,
    roles: tuple[str, ...],
    settings: JwtSettings,
    *,
    now: int | None = None,
) -> str:
    """Build a fresh access JWT.

    `now` is injectable for deterministic tests (mock clock). Production
    callers omit it and rely on `time.time()`.
    """
    issued_at = now if now is not None else int(time.time())
    payload = TokenPayload(
        sub=user_id,
        team_id=team_id,
        roles=roles,
        iat=issued_at,
        exp=issued_at + settings.access_ttl_seconds,
        type="access",
    )
    return encode_token(payload, settings)


def make_refresh_token(
    user_id: UUID,
    settings: JwtSettings,
    *,
    now: int | None = None,
) -> str:
    """Build a fresh refresh JWT.

    Refresh tokens omit team_id/roles — those are re-fetched from DB when
    the refresh endpoint mints a new access token. This protects against
    role grants leaking via a stolen refresh token.
    """
    issued_at = now if now is not None else int(time.time())
    payload = TokenPayload(
        sub=user_id,
        team_id=None,
        roles=(),
        iat=issued_at,
        exp=issued_at + settings.refresh_ttl_seconds,
        type="refresh",
    )
    return encode_token(payload, settings)


__all__ = [
    "InvalidTokenError",
    "JwtSettings",
    "TokenPayload",
    "decode_token",
    "encode_token",
    "make_access_token",
    "make_refresh_token",
]
