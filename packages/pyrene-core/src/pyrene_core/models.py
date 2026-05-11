"""Shared Pydantic primitives. Used across all Pyrene packages.

BRIEF §6.1-1: every tool input/output, agent output, and config inherits from a
strict base model. `dict[str, Any]` is a last resort.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictBaseModel(BaseModel):
    """Project-wide base model. Forbids extra fields; mutation requires explicit copy."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class Confidence(StrEnum):
    """Confidence labels for AnalystResponse (PRD-001 §4.2, F-06)."""

    high = "high"
    medium = "medium"
    low = "low"


class OrderBySpec(StrictBaseModel):
    """Ordering spec for structured SELECT tools (PRD-001 §4.1, PRD-004 §4)."""

    column: str
    direction: Literal["asc", "desc"] = "asc"
