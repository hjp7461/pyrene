"""Regression guard: ADR-019 / F-15 — no `pyrene_*` internal imports.

The package must talk to the gateway via httpx ONLY. Any
`from pyrene_core import ...` or `from pyrene_gateway import ...` would
silently violate the HTTP-only boundary and bypass the hook chain.

ADR-025 amendment: `pyrene_ui_common` is the sole allowed `pyrene_*` import —
a leaf-utility package (httpx/streamlit only, zero domain dep). It carries no
gateway/domain code, so importing it cannot bypass the hook chain.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "pyrene_mcp_frontend"
)

# Self-import + the ADR-025 leaf-utility (`pyrene_ui_common`) are allowed.
# Anything else under `pyrene_` is forbidden.
_FORBIDDEN_PATTERN = re.compile(
    r"^\s*(?:from|import)\s+pyrene_(?!(?:mcp_frontend|ui_common)\b)"
)


def test_no_internal_pyrene_imports() -> None:
    offenders: list[tuple[str, int, str]] = []
    for py in _SRC_DIR.rglob("*.py"):
        for lineno, line in enumerate(
            py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _FORBIDDEN_PATTERN.match(line):
                offenders.append((py.relative_to(_SRC_DIR).as_posix(), lineno, line))
    assert not offenders, (
        "ADR-019 / F-15 violation — pyrene-mcp-frontend must not import "
        "internal pyrene_* modules:\n"
        + "\n".join(f"  {path}:{n}  {line}" for path, n, line in offenders)
    )
