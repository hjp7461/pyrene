"""pyrene-auth runtime settings.

Bundles JWT + DB connection + enumeration-defense sleep. Loaded once at app
startup and injected via FastAPI dependency override in tests.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    """Top-level auth runtime config."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    pg_dsn: str = Field(
        default="postgresql+asyncpg://pyrene:pyrene@localhost:5433/pyrene_auth",
        description="Application DSN (write-capable). Migrations + auth tables.",
    )
    enumeration_defense_ms: int = Field(
        default=200,
        description=(
            "Fixed minimum response time for /auth/login regardless of branch "
            "(user-not-found vs bad-password). Mitigates user enumeration."
        ),
    )


__all__ = ["AuthSettings"]
