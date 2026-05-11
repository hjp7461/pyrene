"""Pyrene cost metering (PRD-013).

Phase 2 surface:
  - `UsageRecord` ORM + `0005_cost_metering` migration.
  - `PricingTable` — YAML-backed model pricing with reload.
  - `make_cost_hook` — `after_run` hook at PRIORITY_AUDIT - 5 (75).
  - Aggregation API (`usage_by_user`, `usage_by_agent`, `usage_by_team`,
    `SummaryCache` with 60s TTL).
  - HTTP routes (`usage_router`, `admin_router`).

Wave 7 constraint: this package does NOT modify the Gateway/agents/auth.
The Gateway-side wiring (hook registration) is the host application's
responsibility — see `make_cost_hook` docs.
"""

from pyrene_metering.aggregation import (
    SummaryCache,
    usage_by_agent,
    usage_by_team,
    usage_by_user,
)
from pyrene_metering.hooks import (
    CostHook,
    TokenUsage,
    UsageExtractor,
    default_usage_extractor,
    make_cost_hook,
)
from pyrene_metering.models import Base, UsageRecord, metadata
from pyrene_metering.pricing import ModelPrice, PricingTable, default_pricing_path
from pyrene_metering.routes import (
    set_pricing_table,
    set_summary_cache,
    usage_router,
)
from pyrene_metering.routes.usage import admin_router
from pyrene_metering.schemas import (
    Period,
    UsageRecordPage,
    UsageRecordResponse,
    UsageSummary,
)

__version__ = "0.1.0"

__all__ = [
    "Base",
    "CostHook",
    "ModelPrice",
    "Period",
    "PricingTable",
    "SummaryCache",
    "TokenUsage",
    "UsageExtractor",
    "UsageRecord",
    "UsageRecordPage",
    "UsageRecordResponse",
    "UsageSummary",
    "admin_router",
    "default_pricing_path",
    "default_usage_extractor",
    "make_cost_hook",
    "metadata",
    "set_pricing_table",
    "set_summary_cache",
    "usage_by_agent",
    "usage_by_team",
    "usage_by_user",
    "usage_router",
]
