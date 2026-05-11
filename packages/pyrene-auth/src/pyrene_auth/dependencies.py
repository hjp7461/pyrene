"""FastAPI dependency factories for auth + RBAC.

`get_current_user` resolves a Bearer token into a `UserContext`:
  1. Decode JWT, validate signature + expiration + type=access
  2. Re-fetch user from DB (rejects soft-deleted / inactive)
  3. Re-fetch roles for the team_id in the token (defends against stale
     role grants if the admin revoked between issue and use)

`require_role(name)` / `require_admin` wrap `get_current_user` and 403 if
the role isn't present. They're factories (return a `Depends`) so route
handlers stay one-line.

All three dependencies expect:
  - `oauth2_scheme` to be injected as the bearer token source
  - the application to provide an `AsyncSession` via `Depends(get_session)`
    where `get_session` is wired by the host app at startup. To keep this
    package free of an opinionated session getter, we expose a
    `set_session_dependency()` hook.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, cast

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from pyrene_auth.jwt import InvalidTokenError, JwtSettings, decode_token
from pyrene_auth.repository import get_active_user_by_id, list_user_roles_for_team
from pyrene_core import UserContext

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


# Host application sets these at startup. Defaults raise to make missing
# wiring a loud failure instead of a silent NPE-style bug.
#
# The session dependency must be a FastAPI-compatible callable: it may be a
# plain `() -> AsyncSession` coroutine, or an async generator `() -> AsyncIterator[AsyncSession]`
# (FastAPI unwraps both). We type it loosely as `Callable[..., Any]` to keep
# host apps free to choose either form.
SessionDependency = Callable[..., Any]
JwtSettingsDependency = Callable[..., JwtSettings]


async def _missing_session_dep() -> AsyncIterator[AsyncSession]:  # pragma: no cover
    raise RuntimeError(
        "pyrene_auth.dependencies.get_session is not configured. "
        "Call `set_session_dependency(...)` at app startup."
    )
    yield  # unreachable; satisfies generator typing


def _default_jwt_settings() -> JwtSettings:
    return JwtSettings()


# Module-level holders so the host app can swap before requests start.
_session_dep: SessionDependency = _missing_session_dep
_jwt_settings_dep: JwtSettingsDependency = _default_jwt_settings


def set_session_dependency(dep: SessionDependency) -> None:
    """Register the host app's `AsyncSession` provider.

    `dep` may be a coroutine returning AsyncSession, or an async generator
    yielding one. FastAPI handles both forms.
    """
    global _session_dep
    _session_dep = dep


def set_jwt_settings_dependency(dep: JwtSettingsDependency) -> None:
    """Register an override for JwtSettings (tests / multi-tenant apps)."""
    global _jwt_settings_dep
    _jwt_settings_dep = dep


def _get_session_dep() -> SessionDependency:
    return _session_dep


def _get_jwt_settings_dep() -> JwtSettingsDependency:
    return _jwt_settings_dep


# Wrapper dependencies that FastAPI can actually resolve. These look up the
# module-level dep at request time so test overrides take effect.
async def _session_proxy() -> AsyncIterator[AsyncSession]:
    inner = _session_dep
    result = inner()
    # Async generator path: iterate once.
    if hasattr(result, "__aiter__"):
        async for session in result:
            yield session
            return
    # Coroutine path: await and yield.
    else:
        session = await cast(Awaitable[AsyncSession], result)
        yield session


def _jwt_settings_proxy() -> JwtSettings:
    return _jwt_settings_dep()


async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    session: AsyncSession = Depends(_session_proxy),
    jwt_settings: JwtSettings = Depends(_jwt_settings_proxy),
) -> UserContext:
    """Decode Bearer token → UserContext.

    Raises 401 on missing/invalid/expired token, or on user soft-deleted /
    inactive. Re-reads roles from DB for the token's team_id so a revoke
    takes effect on the next request (cache layer is PRD-008 future work).
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_token(token, jwt_settings)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if payload.type != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token cannot authenticate requests",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await get_active_user_by_id(session, payload.sub)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.team_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="access token missing team_id",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Re-read roles for defense against stale grants. Cost is one indexed
    # join per request; acceptable for Phase 2.
    roles = await list_user_roles_for_team(session, user.id, payload.team_id)
    return UserContext(user_id=user.id, team_id=payload.team_id, roles=roles)


def require_role(name: str) -> Callable[[UserContext], UserContext]:
    """Dependency factory: 403 if `name` not in `current.roles`."""

    def _dep(
        current: Annotated[UserContext, Depends(get_current_user)],
    ) -> UserContext:
        if name not in current.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{name}' required",
            )
        return current

    return _dep


def require_any_role(*names: str) -> Callable[[UserContext], UserContext]:
    """OR-combination: 403 if none of `names` present in `current.roles`."""
    required = frozenset(names)

    def _dep(
        current: Annotated[UserContext, Depends(get_current_user)],
    ) -> UserContext:
        if required.isdisjoint(current.roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"one of {sorted(required)} required",
            )
        return current

    return _dep


require_admin: Callable[[UserContext], UserContext] = require_role("admin")


__all__ = [
    "get_current_user",
    "oauth2_scheme",
    "require_admin",
    "require_any_role",
    "require_role",
    "set_jwt_settings_dependency",
    "set_session_dependency",
]
