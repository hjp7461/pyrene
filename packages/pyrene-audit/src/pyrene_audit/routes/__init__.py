"""Audit FastAPI routers (PRD-015 §4)."""

from pyrene_audit.routes.events import audit_router

__all__ = ["audit_router"]
