"""Unit tests for `pyrene_gateway.models`.

Asserts table layout / FK targets / uniqueness without requiring a live
Postgres connection (mirror of pyrene-agents/tests/unit/test_models.py
pattern).
"""

from __future__ import annotations

from sqlalchemy import inspect

from pyrene_gateway.models import MCPServer, MCPTool, metadata


def test_metadata_contains_mcp_tables() -> None:
    """Tables registered on the shared MetaData (joined with auth/agents)."""
    table_names = set(metadata.tables.keys())
    assert "mcp_servers" in table_names
    assert "mcp_tools" in table_names


def test_mcp_servers_unique_constraint_team_name() -> None:
    table = MCPServer.__table__
    constraint_names = {c.name for c in table.constraints}  # type: ignore[attr-defined]
    assert "uq_mcp_servers_team_name" in constraint_names


def test_mcp_tools_unique_constraint_server_name() -> None:
    table = MCPTool.__table__
    constraint_names = {c.name for c in table.constraints}  # type: ignore[attr-defined]
    assert "uq_mcp_tools_server_name" in constraint_names


def test_mcp_servers_team_id_fk_target() -> None:
    """ADR-013 (b): team_id ON DELETE CASCADE → teams.id."""
    fk = next(iter(MCPServer.__table__.c.team_id.foreign_keys))
    assert fk.column.table.name == "teams"
    assert fk.ondelete == "CASCADE"


def test_mcp_tools_server_id_fk_target() -> None:
    """ADR-013 (b): server_id ON DELETE CASCADE → mcp_servers.id."""
    fk = next(iter(MCPTool.__table__.c.server_id.foreign_keys))
    assert fk.column.table.name == "mcp_servers"
    assert fk.ondelete == "CASCADE"


def test_mcp_server_instance_construction() -> None:
    """Construct ORM instance to confirm Mapped types resolve."""
    server = MCPServer(
        name="echo",
        transport="stdio",
        command="/usr/bin/echo",
        args=["hello"],
        env={},
    )
    assert server.name == "echo"
    assert server.transport == "stdio"


def test_inspector_columns_present() -> None:
    """Column names are stable — downstream PLANs (010/015) reference them."""
    cols = {c.name for c in inspect(MCPServer).columns}
    assert {
        "id",
        "team_id",
        "name",
        "transport",
        "command",
        "args",
        "env",
        "enabled",
        "created_at",
        "updated_at",
    } <= cols

    cols = {c.name for c in inspect(MCPTool).columns}
    assert {
        "id",
        "server_id",
        "name",
        "description",
        "input_schema",
        "discovered_at",
    } <= cols
