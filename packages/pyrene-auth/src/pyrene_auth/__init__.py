"""Pyrene authentication / RBAC backbone (PRD-007).

Phase 2 entry: defines the `User`, `Team`, `Role`, `UserTeamRole` tables and
the JWT issuance / verification primitives that every downstream Phase 2
package (cost metering, audit, tool-level RBAC) consumes via `UserContext`.

Re-exports the FastAPI routers (`auth_router`, `admin_router`) and dependency
helpers (`get_current_user`, `require_role`, `require_admin`) so downstream
applications can wire the auth surface with a single import.
"""

from pyrene_auth.app import make_app
from pyrene_auth.dependencies import (
    get_current_user,
    oauth2_scheme,
    require_admin,
    require_any_role,
    require_role,
    set_jwt_settings_dependency,
    set_session_dependency,
)
from pyrene_auth.hashing import hash_password, verify_password
from pyrene_auth.jwt import (
    InvalidTokenError,
    JwtSettings,
    TokenPayload,
    decode_token,
    encode_token,
    make_access_token,
    make_refresh_token,
)
from pyrene_auth.models import Base, Role, Team, User, UserTeamRole, metadata
from pyrene_auth.repository import (
    get_active_user_by_id,
    get_or_create_default_team,
    get_role_by_name,
    get_user_by_email,
    list_user_roles_for_team,
)
from pyrene_auth.routes.admin.roles import admin_router
from pyrene_auth.routes.auth import auth_router
from pyrene_auth.settings import AuthSettings

__version__ = "0.1.0"
__all__ = [
    "AuthSettings",
    "Base",
    "InvalidTokenError",
    "JwtSettings",
    "Role",
    "Team",
    "TokenPayload",
    "User",
    "UserTeamRole",
    "admin_router",
    "auth_router",
    "decode_token",
    "encode_token",
    "get_active_user_by_id",
    "get_current_user",
    "get_or_create_default_team",
    "get_role_by_name",
    "get_user_by_email",
    "hash_password",
    "list_user_roles_for_team",
    "make_access_token",
    "make_app",
    "make_refresh_token",
    "metadata",
    "oauth2_scheme",
    "require_admin",
    "require_any_role",
    "require_role",
    "set_jwt_settings_dependency",
    "set_session_dependency",
    "verify_password",
]
