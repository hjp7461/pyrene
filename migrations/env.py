"""Alembic env.py — combines metadata from every Pyrene package that owns DB.

ADR-013 (a): single repo-root alembic config. As new packages introduce
tables (`pyrene-agent-registry`, `pyrene-audit`, `pyrene-cost`, ...), import
their `metadata` here and add it to `combine_metadata([...])`.

DSN resolution priority:
  1. `-x url=...` command-line override (used by testcontainers tests)
  2. `PG_DSN` environment variable
  3. `sqlalchemy.url` from alembic.ini (empty in this repo)
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import cast

from alembic import context
from sqlalchemy import MetaData, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from pyrene_agents.models import metadata as agents_metadata
from pyrene_audit.models import metadata as audit_metadata
from pyrene_auth.models import metadata as auth_metadata
from pyrene_budget.models import metadata as budget_metadata
from pyrene_data_rbac.models import metadata as data_rbac_metadata
from pyrene_gateway.models import metadata as gateway_metadata
from pyrene_metering.models import metadata as metering_metadata
from pyrene_rbac.models import metadata as rbac_metadata


def combine_metadata(items: list[MetaData]) -> MetaData:
    """Merge multiple SQLAlchemy MetaData objects into a single target.

    Each package keeps its own DeclarativeBase; Alembic needs one MetaData
    instance per autogenerate / upgrade. We copy each table into a fresh
    MetaData so packages stay decoupled at the Python layer.
    """
    combined = MetaData()
    for md in items:
        for table in md.tables.values():
            table.to_metadata(combined)
    return combined


# NOTE: pyrene_agents.models reuses pyrene_auth.models.metadata as its
# shared MetaData (see pyrene_agents/models.py); `agents_metadata is
# auth_metadata` at runtime, but importing both pins the table-load order
# so combine_metadata sees both packages' Tables in a single instance.
# The import of `agents_metadata` is what triggers `agent_specs` /
# `agent_versions` to register on the shared MetaData.
_ = agents_metadata  # import side effect: registers agent tables
_ = gateway_metadata  # import side effect: registers mcp_servers / mcp_tools
_ = audit_metadata  # import side effect: registers audit_events
_ = rbac_metadata  # import side effect: registers permissions
_ = data_rbac_metadata  # import side effect: registers data_permissions
_ = metering_metadata  # import side effect: registers usage_records
_ = budget_metadata  # import side effect: registers budget_limits
target_metadata = combine_metadata([auth_metadata])

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    if "url" in x_args:
        return cast(str, x_args["url"])
    env_url = os.environ.get("PG_DSN")
    if env_url:
        return env_url
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    raise RuntimeError(
        "Alembic DSN not configured. Set PG_DSN env var or pass -x url=... on the CLI."
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no connection)."""
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live async connection."""
    url = _resolve_url()
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = url
    engine = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
