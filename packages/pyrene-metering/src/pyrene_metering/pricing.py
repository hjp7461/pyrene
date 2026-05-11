"""Model pricing table loader + cost calculation.

PRD-013 §4: a YAML file maps model identifier → input/output per-token
prices (USD). Cost is computed in `Decimal` (never `float`) so 1k+
row aggregates do not accumulate rounding error (L-02).

### Reload

The YAML is loaded once at startup into a module-level cache. A `reload()`
function re-parses on demand (Day 2 `POST /admin/pricing/reload` endpoint
calls it). The cache is keyed by model id; lookup is O(1).

### Unknown model policy (L-03)

If the model id is not in the table, cost = 0 and a warning is logged.
The request itself is not blocked — availability > strict accounting
(PRD-013 §2.2 F-01).

### Per-million vs per-thousand

The YAML uses `input_per_mtok` / `output_per_mtok` (USD per million
tokens) so the values are human-readable (e.g. `3.00` for Sonnet input)
without leading zeros. Internal arithmetic divides by 1_000_000 in
Decimal land — no float multiplication.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Decimal divisor for per-million-token → per-token conversion. Keep as
# Decimal so the division stays in Decimal arithmetic.
_PER_MTOK_DIVISOR: Decimal = Decimal("1000000")


@dataclass(frozen=True)
class ModelPrice:
    """One row of the pricing table."""

    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    # Cache pricing — most providers charge a fraction of input rate for
    # cache reads. Defaults to 0 (= no extra charge / cache effectively
    # free) when the YAML omits it; concrete numbers ship later.
    cache_read_per_mtok: Decimal = Decimal("0")
    cache_write_per_mtok: Decimal = Decimal("0")


class PricingTable:
    """In-memory pricing cache with reload + thread-safe swap.

    The cache is a `dict[str, ModelPrice]`. `reload()` parses the YAML
    into a new dict and atomically replaces the old one (lock-held
    rebind). Lookups are lock-free reads on the dict reference; Python's
    GIL guarantees the reference swap is atomic. The lock only
    serializes concurrent reload calls.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._table: dict[str, ModelPrice] = {}
        self.reload()

    @property
    def path(self) -> Path:
        return self._path

    def reload(self) -> int:
        """Re-parse the YAML. Returns the number of entries loaded."""
        with self._lock:
            new_table = _parse_yaml(self._path)
            self._table = new_table
            logger.info(
                "metering: pricing table reloaded (%d entries) from %s",
                len(new_table),
                self._path,
            )
            return len(new_table)

    def get(self, model: str) -> ModelPrice | None:
        return self._table.get(model)

    def all_models(self) -> tuple[str, ...]:
        return tuple(sorted(self._table.keys()))

    def compute_cost(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> Decimal:
        """Compute `cost_usd` for one usage event.

        Returns `Decimal("0")` if `model` is not in the table (L-03 unknown
        model policy) — a warning is logged the first time per process via
        the caller's logger filter (we log every miss here; deduplication
        is a downstream concern).
        """
        price = self.get(model)
        if price is None:
            logger.warning(
                "metering: unknown model %r — cost recorded as 0 (L-03)", model
            )
            return Decimal("0")

        # All arithmetic in Decimal — no float coercion.
        in_cost = price.input_per_mtok * Decimal(input_tokens) / _PER_MTOK_DIVISOR
        out_cost = price.output_per_mtok * Decimal(output_tokens) / _PER_MTOK_DIVISOR
        cache_r = (
            price.cache_read_per_mtok * Decimal(cache_read_tokens) / _PER_MTOK_DIVISOR
        )
        cache_w = (
            price.cache_write_per_mtok
            * Decimal(cache_write_tokens)
            / _PER_MTOK_DIVISOR
        )
        return in_cost + out_cost + cache_r + cache_w


def _parse_yaml(path: Path) -> dict[str, ModelPrice]:
    """Parse the YAML file into the in-memory table.

    Schema:
        - model: <id>
          input_per_mtok: <number>          # USD per 1M input tokens
          output_per_mtok: <number>
          cache_read_per_mtok: <number>     # optional, default 0
          cache_write_per_mtok: <number>    # optional, default 0
    """
    if not path.exists():
        raise FileNotFoundError(f"pricing file not found: {path}")

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(
            f"pricing file must be a list of entries; got {type(raw).__name__}"
        )

    table: dict[str, ModelPrice] = {}
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(
                f"pricing entry #{idx} is not a mapping: {entry!r}"
            )
        try:
            model = str(entry["model"])
            in_p = Decimal(str(entry["input_per_mtok"]))
            out_p = Decimal(str(entry["output_per_mtok"]))
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"pricing entry #{idx} missing required field: {exc}"
            ) from exc

        cache_r = Decimal(str(entry.get("cache_read_per_mtok", "0")))
        cache_w = Decimal(str(entry.get("cache_write_per_mtok", "0")))

        table[model] = ModelPrice(
            model=model,
            input_per_mtok=in_p,
            output_per_mtok=out_p,
            cache_read_per_mtok=cache_r,
            cache_write_per_mtok=cache_w,
        )
    return table


def default_pricing_path() -> Path:
    """Return the path to the bundled `model_pricing.yaml`.

    Phase 2 stub — Day 1 ships approximate values. The real numbers are
    operator-tunable via the file.
    """
    return Path(__file__).parent / "config" / "model_pricing.yaml"


__all__ = [
    "ModelPrice",
    "PricingTable",
    "default_pricing_path",
]
