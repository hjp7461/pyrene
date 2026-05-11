"""Unit tests for `PricingTable` cost computation.

Covers:
  - exact Decimal arithmetic (no float rounding error)
  - 1k-row accumulation has zero error (L-02 / PRD-013 §7)
  - unknown model → cost 0 (L-03)
  - reload() picks up file changes
  - cache_read/write tokens contribute correctly
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pyrene_metering import PricingTable, default_pricing_path


def test_default_pricing_loads() -> None:
    """The bundled YAML parses to 3 known models."""
    table = PricingTable(default_pricing_path())
    models = table.all_models()
    assert "anthropic:claude-sonnet-4-6" in models
    assert "anthropic:claude-opus-4-7" in models
    assert "openai:gpt-5" in models


def test_sonnet_input_output_cost_exact() -> None:
    """1M input + 1M output tokens against Sonnet rates = $3 + $15 = $18 exactly."""
    table = PricingTable(default_pricing_path())
    cost = table.compute_cost(
        model="anthropic:claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == Decimal("18.00000000")


def test_opus_input_output_cost_exact() -> None:
    """1M + 1M against Opus rates = $15 + $75 = $90 exactly."""
    table = PricingTable(default_pricing_path())
    cost = table.compute_cost(
        model="anthropic:claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == Decimal("90.00000000")


def test_small_token_count_sub_cent_precision() -> None:
    """1 input token against Sonnet ($3/M) = $0.000003 — sub-cent representable."""
    table = PricingTable(default_pricing_path())
    cost = table.compute_cost(
        model="anthropic:claude-sonnet-4-6",
        input_tokens=1,
        output_tokens=0,
    )
    # 3 USD / 1_000_000 = 0.000003
    assert cost == Decimal("0.000003")


def test_cache_read_tokens_priced() -> None:
    """Cache read tokens are priced at the cache_read_per_mtok rate (Sonnet: $0.30/M)."""
    table = PricingTable(default_pricing_path())
    cost = table.compute_cost(
        model="anthropic:claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cost == Decimal("0.30000000")


def test_cache_write_tokens_priced() -> None:
    """Cache write tokens priced at cache_write_per_mtok (Sonnet: $3.75/M)."""
    table = PricingTable(default_pricing_path())
    cost = table.compute_cost(
        model="anthropic:claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=1_000_000,
    )
    assert cost == Decimal("3.75000000")


def test_unknown_model_returns_zero(caplog: pytest.LogCaptureFixture) -> None:
    """L-03: unknown model id → cost 0, warning logged, no exception.

    We attach a list-collecting handler directly to the pricing module's
    logger. Because alembic's `fileConfig(alembic.ini)` runs during the
    integration test session and disables existing loggers (Python
    logging default), we force-enable + force-set the level here so the
    cross-test-run ordering does not affect this unit test.
    """
    import logging

    from pyrene_metering import pricing as pricing_mod

    table = PricingTable(default_pricing_path())
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    pricing_mod.logger.addHandler(handler)
    prev_level = pricing_mod.logger.level
    prev_disabled = pricing_mod.logger.disabled
    pricing_mod.logger.setLevel(logging.WARNING)
    pricing_mod.logger.disabled = False
    try:
        cost = table.compute_cost(
            model="anthropic:claude-mystery-9",
            input_tokens=10_000,
            output_tokens=10_000,
        )
    finally:
        pricing_mod.logger.removeHandler(handler)
        pricing_mod.logger.setLevel(prev_level)
        pricing_mod.logger.disabled = prev_disabled

    assert cost == Decimal("0")
    assert any("unknown model" in rec.getMessage() for rec in records), [
        rec.getMessage() for rec in records
    ]
    # Smoke-check that caplog is still wired (no assertion — just keep
    # the fixture in the signature so we know the API is exercised).
    _ = caplog


def test_zero_token_cost_is_exact_zero() -> None:
    """0 tokens against any model = Decimal('0'), not a near-zero float."""
    table = PricingTable(default_pricing_path())
    cost = table.compute_cost(
        model="anthropic:claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
    )
    # Decimal('0') compares equal to Decimal('0.00000000')
    assert cost == Decimal("0")


def test_accumulation_no_rounding_error() -> None:
    """1000 identical small charges sum to exactly 1000x the unit cost.

    This is the L-02 anchor — Decimal arithmetic must not drift across
    accumulation. Compare against the analytic answer to 8 decimal places.
    """
    table = PricingTable(default_pricing_path())
    unit = table.compute_cost(
        model="anthropic:claude-sonnet-4-6",
        input_tokens=123,
        output_tokens=456,
    )
    total = sum((unit for _ in range(1000)), Decimal("0"))
    expected = unit * Decimal(1000)
    assert total == expected
    # And the analytic check: 123 * 3 / 1M + 456 * 15 / 1M, * 1000
    analytic = (
        Decimal(123) * Decimal("3.00") / Decimal("1000000")
        + Decimal(456) * Decimal("15.00") / Decimal("1000000")
    ) * Decimal(1000)
    assert total == analytic


def test_reload_picks_up_changes(tmp_path: Path) -> None:
    """`reload()` re-reads the YAML and the new prices apply on the next compute."""
    p = tmp_path / "pricing.yaml"
    p.write_text(
        "- model: test-model\n"
        "  input_per_mtok: 1.00\n"
        "  output_per_mtok: 2.00\n"
    )
    table = PricingTable(p)
    cost_before = table.compute_cost(
        model="test-model",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost_before == Decimal("1.00000000")

    # Bump the input rate to 10.
    p.write_text(
        "- model: test-model\n"
        "  input_per_mtok: 10.00\n"
        "  output_per_mtok: 2.00\n"
    )
    n = table.reload()
    assert n == 1
    cost_after = table.compute_cost(
        model="test-model",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost_after == Decimal("10.00000000")


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    """A YAML file missing required fields raises a clear error."""
    p = tmp_path / "bad.yaml"
    p.write_text("- model: foo\n")  # missing input/output prices
    with pytest.raises(ValueError, match="missing required field"):
        PricingTable(p)


def test_missing_file_raises(tmp_path: Path) -> None:
    """A non-existent path raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        PricingTable(tmp_path / "does-not-exist.yaml")
