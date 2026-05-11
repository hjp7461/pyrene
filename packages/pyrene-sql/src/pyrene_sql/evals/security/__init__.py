"""Pyrene security evals — bypass / cost / permission scenario harness.

PLAN-017 / PRD-017. Sibling of `pyrene_sql.evals` (PLAN-005) but with
**deterministic-only** evaluation:

  - No LLM judge — every signal is boolean / integer.
  - No real-model call by default (the `security-evals` CI stage is
    mocked per ADR-012 stage 6).
  - 42 cases across three categories:
      - bypass (17): SQL injection, encoded payload, tool-name spoof,
        jailbreak, schema-qualified, JOIN cross-tenant, search_path
      - cost (13): token burn, self-correction loop, budget race
        (asyncio.Event barrier + exact denial count), retry cap,
        streaming, temperature override, multi-agent fan-out
      - permission (12): viewer-on-admin, cross-team, deny precedence,
        cross-team via shared MCP, audit-log read, admin escalation,
        search_path, stale JWT (xfail — PLAN-007 has no denylist),
        stale cache

Public surface re-exported for tooling and tests:
"""

from pyrene_sql.evals.security.baselines import (
    BaselineEntry,
    assert_matches_baseline,
    baseline_path,
    load_baseline,
    write_baseline,
)
from pyrene_sql.evals.security.driver import (
    DriverOutcome,
    MockSecurityDriver,
    expected_race_outcome,
)
from pyrene_sql.evals.security.evaluator import SecurityEvaluator
from pyrene_sql.evals.security.loader import (
    load_all_security_datasets,
    load_security_dataset,
    security_dataset_path,
)
from pyrene_sql.evals.security.models import (
    SecurityCategory,
    SecurityEvalCase,
    SecurityEvalExpectation,
    SecurityEvalResult,
)

__all__ = [
    "BaselineEntry",
    "DriverOutcome",
    "MockSecurityDriver",
    "SecurityCategory",
    "SecurityEvalCase",
    "SecurityEvalExpectation",
    "SecurityEvalResult",
    "SecurityEvaluator",
    "assert_matches_baseline",
    "baseline_path",
    "expected_race_outcome",
    "load_all_security_datasets",
    "load_baseline",
    "load_security_dataset",
    "security_dataset_path",
    "write_baseline",
]
