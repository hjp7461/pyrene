"""CLI for pyrene-agents.

Single subcommand for Phase 2:
  - `pyrene-agents export-phase1 --output <path>`: dump the Phase 1
    sql-analyst spec to a yaml file (round-trippable via the registry API).

Argument parsing is hand-rolled to keep the dependency footprint identical
to `pyrene-auth.cli` (no Typer / Click pulled into the build).
"""

from __future__ import annotations

import sys
from pathlib import Path

from pyrene_agents.exporter import export_phase1_yaml


def app(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] != "export-phase1":
        sys.stderr.write(
            "usage: pyrene-agents export-phase1 [--output PATH]\n"
        )
        return 2

    output: Path | None = None
    it = iter(args[1:])
    for token in it:
        if token == "--output":
            value = next(it, None)
            if value is None:
                sys.stderr.write("--output requires a value\n")
                return 2
            output = Path(value)

    if output is None:
        output = Path("specs/phase1.yaml")

    export_phase1_yaml(output)
    sys.stdout.write(f"wrote phase1 spec to {output}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
