"""Pyrene MCP gateway (PRD-009).

Phase 2 backbone:
  - MCP server / tool catalog models (`MCPServer`, `MCPTool`).
  - Stdio MCP subprocess client + tool discovery.
  - `Gateway` hook chain (canonical 5-stage priority schedule).

Public surface is intentionally small — PLAN-010/011/013/014/015
import the hook Protocols and PRIORITY_* constants from here and
register their hooks at app startup.
"""

from pyrene_gateway.constants import (
    PRIORITY_AUDIT,
    PRIORITY_BUDGET_POST,
    PRIORITY_BUDGET_PRE,
    PRIORITY_DATA_RBAC,
    PRIORITY_TOOL_RBAC,
)
from pyrene_gateway.context import RunContext
from pyrene_gateway.gateway import Gateway
from pyrene_gateway.hooks import AfterRunHook, BeforeRunHook, HookRegistry
from pyrene_gateway.mcp_client import (
    DiscoveredTool,
    McpStartupError,
    McpToolError,
    StdioMcpClient,
)
from pyrene_gateway.models import Base, MCPServer, MCPTool, metadata
from pyrene_gateway.routes import servers_router
from pyrene_gateway.schemas import (
    MCPServerCreate,
    MCPServerResponse,
    MCPToolResponse,
    MCPTransport,
)
from pyrene_gateway.tool_discovery import discover_tools

__version__ = "0.1.0"

__all__ = [
    "PRIORITY_AUDIT",
    "PRIORITY_BUDGET_POST",
    "PRIORITY_BUDGET_PRE",
    "PRIORITY_DATA_RBAC",
    "PRIORITY_TOOL_RBAC",
    "AfterRunHook",
    "Base",
    "BeforeRunHook",
    "DiscoveredTool",
    "Gateway",
    "HookRegistry",
    "MCPServer",
    "MCPServerCreate",
    "MCPServerResponse",
    "MCPTool",
    "MCPToolResponse",
    "MCPTransport",
    "McpStartupError",
    "McpToolError",
    "RunContext",
    "StdioMcpClient",
    "discover_tools",
    "metadata",
    "servers_router",
]
