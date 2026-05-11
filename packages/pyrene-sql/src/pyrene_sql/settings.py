"""Pyrene SQL runtime settings. Loaded from env / .env.

PG_DSN vs PG_READONLY_DSN: F-03 이중 방어 — 도구 호출은 readonly DSN만 본다.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    pg_dsn: str = Field(
        ...,
        description="Application connection (read+write). Used only for migrations.",
    )
    pg_readonly_dsn: str = Field(
        ...,
        description="Read-only connection used by run_select and friends (F-03).",
    )
    model_name: str = Field(
        default="anthropic:claude-sonnet-4-6",
        description=(
            "Pydantic AI model identifier (provider:model). Uses anthropic:* by "
            "default; switch to e.g. 'openai:gpt-5' via .env without code change."
        ),
    )
    anthropic_api_key: str | None = Field(default=None)
    logfire_token: str | None = Field(default=None)
    logfire_project_name: str = Field(default="pyrene-sql")
