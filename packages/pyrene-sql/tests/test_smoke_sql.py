"""Smoke tests for the pyrene-sql package.

These cover only what can be exercised without a DB / model:
  - the package imports cleanly
  - the typer app is wired and `--help` enumerates the commands
"""

from __future__ import annotations


def test_import() -> None:
    import pyrene_sql

    assert pyrene_sql.__version__ == "0.1.0"


def test_cli_help_lists_commands() -> None:
    from typer.testing import CliRunner

    from pyrene_sql.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Both commands should show up in the help output.
    assert "ask" in result.stdout
    assert "index-schema" in result.stdout
