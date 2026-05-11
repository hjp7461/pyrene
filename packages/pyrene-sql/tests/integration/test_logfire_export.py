"""Integration test: end-to-end Logfire HTTP export verification.

PLAN-006 §5 — only runs when both LOGFIRE_TOKEN *and* a Logfire read API
key are present. Skipped on every CI run that doesn't ship those secrets
so the test environment stays free of network requirements.

The test:
  1. Configure Logfire with the project token + service_name="pyrene-test"
  2. Emit a `pyrene.sql.run_select` span (no DB required)
  3. Wait briefly for the OTel batch processor to flush
  4. Hit the Logfire query API and assert the span is reachable

Logfire's HTTP API is documented at https://logfire.pydantic.dev/docs/
reference/api/ — the read-side endpoint requires a separate "read token"
distinct from the project write token. We honor both `LOGFIRE_TOKEN`
(write) and `LOGFIRE_READ_TOKEN` (read) env vars.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.getenv("LOGFIRE_TOKEN") and os.getenv("LOGFIRE_READ_TOKEN")),
        reason=(
            "Requires LOGFIRE_TOKEN (write) + LOGFIRE_READ_TOKEN (read). "
            "PLAN-006 §5: only runs when nightly secrets are provisioned."
        ),
    ),
]


def _query_logfire(
    *, read_token: str, service_name: str, since_minutes: int = 5
) -> dict[str, Any]:
    """Query Logfire for recent spans on `service_name`. Returns parsed JSON."""
    url = "https://logfire-api.pydantic.dev/v1/query"
    headers = {"Authorization": f"Bearer {read_token}"}
    params = {
        "sql": (
            "SELECT span_name, attributes "
            "FROM records "
            f"WHERE service_name = '{service_name}' "
            f"AND start_timestamp > now() - interval '{since_minutes} minutes'"
        )
    }
    response = httpx.get(url, headers=headers, params=params, timeout=15.0)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


async def test_logfire_export_round_trip() -> None:
    """Emit a span → flush → query Logfire HTTP API → assert presence."""
    import logfire

    from pyrene_core import SPAN_SQL_RUN_SELECT, configure_logfire

    service_name = "pyrene-logfire-export-test"
    configure_logfire(
        service_name=service_name,
        send_to_logfire="always",
    )

    with logfire.span(
        SPAN_SQL_RUN_SELECT,
        table="public.film",
        limit=5,
    ) as span:
        span.set_attribute("row_count", 1)
        span.set_attribute("truncated", False)

    # Force the batch processor to flush before we hit the read API.
    logfire.force_flush()
    # Logfire's ingestion pipeline takes a couple seconds to make spans
    # queryable. We poll up to 30s.
    deadline = time.monotonic() + 30
    read_token = os.environ["LOGFIRE_READ_TOKEN"]

    while time.monotonic() < deadline:
        body = _query_logfire(read_token=read_token, service_name=service_name)
        # The Logfire query API returns a `rows` array.
        rows = body.get("rows") or body.get("data") or []
        names = {row.get("span_name") for row in rows if isinstance(row, dict)}
        if SPAN_SQL_RUN_SELECT in names:
            assert len(rows) > 0
            return
        time.sleep(2)

    pytest.fail(
        f"span '{SPAN_SQL_RUN_SELECT}' did not appear in Logfire query "
        f"results for service_name={service_name} within 30s"
    )
