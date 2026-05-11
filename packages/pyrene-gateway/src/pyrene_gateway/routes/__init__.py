"""HTTP surface for the MCP gateway.

`servers_router` — admin-only MCP server registration + tool discovery.
"""

from pyrene_gateway.routes.servers import servers_router

__all__ = ["servers_router"]
