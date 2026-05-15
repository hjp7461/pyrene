"""PRD-046 §6.2 — `should_offer_retry` decision rule.

F-04's retry policy (N1 empty / N2 timeout / N3 refusal) is decoded from
`AnalystRunResult`'s top-level fields (refusal / row_count) — *not* the
per-attempt error string (which is just a message, no exception type).

Decision table (PRD §6.1):
  | exc          | resp.refusal | resp.row_count | retry? |
  |--------------|--------------|----------------|--------|
  | not None     | —            | —              | True   | (network/parse)
  | None         | not None     | —              | False  | (refusal = answer)
  | None         | None         | 0 (rows ≠ None)| False  | (empty — F-04 N1)
  | None         | None         | anything else  | True   |
"""

from __future__ import annotations

from pyrene_mcp_frontend.agent_client import AnalystRunResult


def should_offer_retry(
    *,
    resp: AnalystRunResult | None,
    exc: Exception | None,
) -> bool:
    """Whether to show the 🔄 재시도 button after a run.

    True: surface the retry button. False: hide it (showing it would be
    misleading — refusal *is* the answer, or an empty result won't change
    by re-issuing the same prompt).
    """
    if exc is not None:
        return True
    if resp is None:
        return True
    if resp.refusal is not None:
        return False
    return not (resp.rows is not None and resp.row_count == 0)


__all__ = ["should_offer_retry"]
