"""FastAPI routers for the agent registry."""

from pyrene_agents.routes.run import run_router
from pyrene_agents.routes.specs import specs_router

__all__ = ["run_router", "specs_router"]
