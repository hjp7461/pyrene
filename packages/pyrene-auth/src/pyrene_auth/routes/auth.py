"""Auth API surface: signup / login / refresh / me.

User-enumeration defense (PRD-007 §6, BRIEF §1.2):

  - `/auth/login` returns the same 401 + same body whether the email is
    unknown or the password is wrong. We also enforce a minimum response
    time via `_enumeration_floor()` so the timing side channel doesn't
    distinguish "user not found" (~1 ms) from "argon2 verify miss"
    (~50-200 ms depending on cost params).
  - `/auth/signup` is permissive (no email enumeration on signup is the
    standard portfolio trade-off — admins control account creation in
    Phase 2 anyway).
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.dependencies import (
    _jwt_settings_proxy,
    _session_proxy,
    get_current_user,
)
from pyrene_auth.hashing import hash_password, verify_password
from pyrene_auth.jwt import (
    InvalidTokenError,
    JwtSettings,
    decode_token,
    make_access_token,
    make_refresh_token,
)
from pyrene_auth.models import User
from pyrene_auth.repository import (
    get_active_user_by_id,
    get_or_create_default_team,
    get_user_by_email,
    list_user_roles_for_team,
)
from pyrene_auth.schemas import (
    AccessTokenResponse,
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenPairResponse,
    UserResponse,
)
from pyrene_auth.settings import AuthSettings
from pyrene_core import UserContext

auth_router = APIRouter(prefix="/auth", tags=["auth"])


# Sentinel argon2 hash used on the user-not-found branch of /auth/login so
# verify_password still runs and consumes the same CPU as a real verify.
# Generated once at import time from a random password — its plaintext is
# never compared, only the hash structure exists to feed argon2's verify.
from pyrene_auth.hashing import hash_password as _hp  # noqa: E402

_DUMMY_HASH = _hp("user-enumeration-defense-sentinel")


def _auth_settings_dep() -> AuthSettings:  # pragma: no cover - default factory
    return AuthSettings()


async def _enumeration_floor(start_time_ns: int, floor_ms: int) -> None:
    """Sleep until at least `floor_ms` have passed since `start_time_ns`."""
    import asyncio

    elapsed_ms = (time.monotonic_ns() - start_time_ns) / 1_000_000
    remaining = max(0.0, floor_ms - elapsed_ms)
    if remaining > 0:
        await asyncio.sleep(remaining / 1000)


@auth_router.post(
    "/signup",
    response_model=TokenPairResponse,
    status_code=status.HTTP_201_CREATED,
)
async def signup(
    body: SignupRequest,
    session: AsyncSession = Depends(_session_proxy),
    jwt_settings: JwtSettings = Depends(_jwt_settings_proxy),
) -> TokenPairResponse:
    existing = await get_user_by_email(session, body.email)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already taken")

    team = await get_or_create_default_team(session)
    user = User(email=body.email, password_hash=hash_password(body.password))
    session.add(user)
    await session.flush()
    await session.commit()

    roles = await list_user_roles_for_team(session, user.id, team.id)
    access = make_access_token(user.id, team.id, roles, jwt_settings)
    refresh = make_refresh_token(user.id, jwt_settings)
    return TokenPairResponse(access_token=access, refresh_token=refresh)


@auth_router.post("/login", response_model=TokenPairResponse)
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(_session_proxy),
    jwt_settings: JwtSettings = Depends(_jwt_settings_proxy),
    auth_settings: AuthSettings = Depends(_auth_settings_dep),
) -> TokenPairResponse:
    """Email + password → token pair.

    Constant-time defense: both branches (user-not-found, bad-password) run
    the same code path (argon2 verify against a real hash) and the response
    is delayed to at least `enumeration_defense_ms`.
    """
    start = time.monotonic_ns()

    user = await get_user_by_email(session, body.email)
    if user is None:
        # Run verify against dummy hash so timing matches the real branch.
        verify_password(body.password, _DUMMY_HASH)
        await _enumeration_floor(start, auth_settings.enumeration_defense_ms)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_password(body.password, user.password_hash):
        await _enumeration_floor(start, auth_settings.enumeration_defense_ms)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    team = await get_or_create_default_team(session)
    roles = await list_user_roles_for_team(session, user.id, team.id)
    access = make_access_token(user.id, team.id, roles, jwt_settings)
    refresh = make_refresh_token(user.id, jwt_settings)
    # Even success goes through the floor so any login takes a similar time.
    await _enumeration_floor(start, auth_settings.enumeration_defense_ms)
    return TokenPairResponse(access_token=access, refresh_token=refresh)


@auth_router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    body: RefreshRequest,
    session: AsyncSession = Depends(_session_proxy),
    jwt_settings: JwtSettings = Depends(_jwt_settings_proxy),
) -> AccessTokenResponse:
    try:
        payload = decode_token(body.refresh_token, jwt_settings)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    if payload.type != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not a refresh token"
        )

    user = await get_active_user_by_id(session, payload.sub)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token"
        )

    team = await get_or_create_default_team(session)
    roles = await list_user_roles_for_team(session, user.id, team.id)
    access = make_access_token(user.id, team.id, roles, jwt_settings)
    return AccessTokenResponse(access_token=access)


@auth_router.get("/me", response_model=UserResponse)
async def me(
    current: Annotated[UserContext, Depends(get_current_user)],
    session: AsyncSession = Depends(_session_proxy),
) -> UserResponse:
    user = await get_active_user_by_id(session, current.user_id)
    if user is None:  # pragma: no cover - get_current_user already guards
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found"
        )
    return UserResponse(
        id=user.id,
        email=user.email,
        team_id=current.team_id,
        roles=current.roles,
    )


__all__ = ["auth_router"]
