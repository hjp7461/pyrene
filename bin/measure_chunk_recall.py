#!/usr/bin/env python
"""3-way A/B/C recall measurement for the schema-RAG retriever.

PRD-043 — production OpenAI embedding 환경에서 chunk strategy 와
HNSW ef_search 정책의 *실제 effect* 측정. 같은 (Hybrid emit) 인덱스
위에서 *retrieval 만 variant 별 분기* 해 공정 비교.

Variants:
  A. Hybrid + ef_search=200  — current production state
  B. Hybrid + ef_search=100  — PRD-041 원복 시뮬 (PRD-044 OQ-7 시드)
  C. Pure (table-only) + ef_search=200  — Hybrid 효과 비교 baseline

Pure helpers (load_dataset / compute_accuracy / render_markdown_report)
are unit-tested in `packages/pyrene-sql/tests/unit/test_measure_helpers.py`;
the IO-heavy `measure_one_variant` is exercised only by live runs.

Usage:
    docker compose up -d
    docker compose exec pyrene-api uv run alembic upgrade head
    docker compose exec pyrene-api uv run pyrene-sql index-schema --reindex
    OPENAI_API_KEY=sk-... PG_DSN=... uv run python bin/measure_chunk_recall.py \\
        --output docs/measurements/2026-05-14-recall-baseline.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from pyrene_sql.schema.embeddings import EMBEDDING_DIMENSIONS, OpenAIEmbedder
from pyrene_sql.schema.models import DEFAULT_CONNECTION_ID

# ---------------------------------------------------------------------------
# Variant catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One measurement configuration."""

    key: str
    label: str
    description: str
    ef_search: int
    chunk_type_filter: str | None  # None = no filter (Hybrid retrieval)


VARIANTS: tuple[Variant, ...] = (
    Variant(
        key="A",
        label="Hybrid + ef_search=200",
        description="현재 production state (PRD-042 + PRD-041)",
        ef_search=200,
        chunk_type_filter=None,
    ),
    Variant(
        key="B",
        label="Hybrid + ef_search=100",
        description="PRD-041 원복 시뮬 (PRD-044 OQ-7 시드)",
        ef_search=100,
        chunk_type_filter=None,
    ),
    Variant(
        key="C",
        label="Pure (table-only) + ef_search=200",
        description="Hybrid 효과 비교 baseline (PRD-042 효과 입증)",
        ef_search=200,
        chunk_type_filter="table",
    ),
)


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissRecord:
    case_id: str
    query: str
    expected: tuple[str, ...]
    got_top_3: tuple[str, ...]


@dataclass(frozen=True)
class VariantResult:
    variant: Variant
    top_3_accuracy: float
    top_5_accuracy: float
    misses: tuple[MissRecord, ...]
    latency_p50_ms: float
    latency_p95_ms: float
    total_cases: int


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable)
# ---------------------------------------------------------------------------


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """Load `schema_retrieval_30.yaml` and assert the 30-case invariant."""
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    cases: list[dict[str, Any]] = loaded["cases"]
    if len(cases) != 30:
        raise ValueError(f"expected 30 cases in {path}, got {len(cases)}")
    return cases


def compute_accuracy(hits: int, total: int) -> float:
    """`hits / total` with a guard for `total == 0`."""
    if total <= 0:
        return 0.0
    return hits / total


def _percentile(values: list[float], p: float) -> float:
    """Inclusive percentile via `statistics.quantiles` for n>=2, else median."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    idx = max(0, min(98, round(p) - 1))
    return quantiles[idx]


def render_markdown_report(
    *,
    git_sha: str,
    timestamp_iso: str,
    embedder_model: str,
    embedder_dimensions: int,
    dataset_path: str,
    chunk_count: int,
    table_count: int,
    column_count: int,
    results: tuple[VariantResult, ...],
) -> str:
    """Render the measurement results as a deterministic markdown blob."""
    lines: list[str] = []
    lines.append(f"# Recall measurement — {timestamp_iso}")
    lines.append("")
    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| git SHA | `{git_sha}` |")
    lines.append(f"| OpenAI model | {embedder_model} @ {embedder_dimensions}-dim |")
    lines.append(f"| dataset | `{dataset_path}` (30 cases) |")
    lines.append(
        f"| index | {table_count} BASE TABLES + {column_count} columns "
        f"= {chunk_count} chunks |"
    )
    lines.append("")
    lines.append("## Accuracy")
    lines.append("")
    lines.append("| Variant | top-3 | top-5 | latency_p50 | latency_p95 |")
    lines.append("|---------|-------|-------|-------------|-------------|")
    for r in results:
        lines.append(
            f"| **{r.variant.key}.** {r.variant.label} "
            f"| {r.top_3_accuracy:.1%} | {r.top_5_accuracy:.1%} "
            f"| {r.latency_p50_ms:.1f}ms | {r.latency_p95_ms:.1f}ms |"
        )
    lines.append("")
    lines.append("## Variant descriptions")
    lines.append("")
    for r in results:
        lines.append(f"- **{r.variant.key}.** {r.variant.label} — {r.variant.description}")
    lines.append("")
    lines.append("## Misses by variant")
    lines.append("")
    for r in results:
        lines.append(f"### {r.variant.key}. {r.variant.label}")
        lines.append("")
        if not r.misses:
            lines.append("- (no misses — 100% top-3 accuracy)")
        else:
            for m in r.misses:
                lines.append(
                    f"- [{m.case_id}] '{m.query}' "
                    f"expected one of {list(m.expected)}, "
                    f"got top-3 {list(m.got_top_3)}"
                )
        lines.append("")
    lines.append("## 결론 (사용자 검토 후 수정)")
    lines.append("")
    by_key = {r.variant.key: r for r in results}
    a = by_key.get("A")
    b = by_key.get("B")
    c = by_key.get("C")
    if a and c:
        delta_hybrid = a.top_3_accuracy - c.top_3_accuracy
        lines.append(
            f"- **Hybrid 효과**: A ({a.top_3_accuracy:.1%}) vs C "
            f"({c.top_3_accuracy:.1%}) → **{delta_hybrid:+.1%}p**"
        )
    if a and b:
        delta_ef = a.top_3_accuracy - b.top_3_accuracy
        lines.append(
            f"- **ef_search 영향**: A ({a.top_3_accuracy:.1%}, ef=200) vs B "
            f"({b.top_3_accuracy:.1%}, ef=100) → **{delta_ef:+.1%}p**"
        )
        if abs(delta_ef) < 0.01:
            lines.append("- **PRD-044 권장**: ef_search 200 → 100 원복 안전 (Δ < 1%p)")
        else:
            lines.append(
                "- **PRD-044 권장**: ef_search 200 유지 — "
                f"100 으로 원복 시 {-delta_ef:+.1%}p 손실"
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# IO-heavy core (live-only)
# ---------------------------------------------------------------------------


async def _vectorize(embedder: OpenAIEmbedder, query: str) -> str:
    embeddings = await embedder.embed([query])
    if not embeddings:
        raise RuntimeError("embedder returned empty result")
    vec = embeddings[0]
    if len(vec) != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"embedder returned {len(vec)} dims, expected {EMBEDDING_DIMENSIONS}"
        )
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


async def _select_top_k(
    *,
    session: AsyncSession,
    vector_literal: str,
    connection_id: UUID,
    k: int,
    ef_search: int,
    chunk_type_filter: str | None,
) -> list[tuple[Any, ...]]:
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
    base_sql = """
        SELECT schema, "table", chunk_type, column_name
          FROM pyrene_schema_embeddings
         WHERE connection_id = :cid
    """
    if chunk_type_filter is not None:
        base_sql += "   AND chunk_type = :ct"
    base_sql += """
         ORDER BY embedding <=> CAST(:qv AS vector),
                  schema ASC,
                  "table" ASC,
                  column_name ASC
         LIMIT :k
    """
    params: dict[str, Any] = {
        "cid": connection_id,
        "qv": vector_literal,
        "k": k,
    }
    if chunk_type_filter is not None:
        params["ct"] = chunk_type_filter
    result = await session.execute(text(base_sql), params)
    return [tuple(row) for row in result.fetchall()]


async def measure_one_variant(
    *,
    session: AsyncSession,
    embedder: OpenAIEmbedder,
    cases: list[dict[str, Any]],
    variant: Variant,
    k_top: int = 5,
    connection_id: UUID = DEFAULT_CONNECTION_ID,
) -> VariantResult:
    """Run all 30 cases against one variant and aggregate metrics."""
    hits_top_3 = 0
    hits_top_5 = 0
    misses: list[MissRecord] = []
    latencies_ms: list[float] = []

    for case in cases:
        case_id = str(case["id"])
        query = str(case["query"])
        expected = tuple(str(t) for t in case["expected_tables"])
        vec = await _vectorize(embedder, query)
        start = time.perf_counter()
        rows = await _select_top_k(
            session=session,
            vector_literal=vec,
            connection_id=connection_id,
            k=k_top,
            ef_search=variant.ef_search,
            chunk_type_filter=variant.chunk_type_filter,
        )
        latencies_ms.append((time.perf_counter() - start) * 1000.0)

        # Compare on table identity (chunk_type-independent: a column chunk
        # for the right table still counts as a hit).
        got_tables = tuple(str(row[1]) for row in rows)
        got_top_3 = got_tables[:3]
        if any(t in got_top_3 for t in expected):
            hits_top_3 += 1
        else:
            misses.append(
                MissRecord(
                    case_id=case_id,
                    query=query,
                    expected=expected,
                    got_top_3=got_top_3,
                )
            )
        if any(t in got_tables[:5] for t in expected):
            hits_top_5 += 1

    return VariantResult(
        variant=variant,
        top_3_accuracy=compute_accuracy(hits_top_3, len(cases)),
        top_5_accuracy=compute_accuracy(hits_top_5, len(cases)),
        misses=tuple(misses),
        latency_p50_ms=_percentile(latencies_ms, 50),
        latency_p95_ms=_percentile(latencies_ms, 95),
        total_cases=len(cases),
    )


async def _index_chunk_summary(
    session: AsyncSession, connection_id: UUID
) -> tuple[int, int, int]:
    """Return (total_chunks, table_chunks, column_chunks) — for the markdown header."""
    total = (
        await session.execute(
            text(
                "SELECT count(*) FROM pyrene_schema_embeddings "
                "WHERE connection_id = :cid"
            ),
            {"cid": connection_id},
        )
    ).scalar_one()
    table_count = (
        await session.execute(
            text(
                "SELECT count(*) FROM pyrene_schema_embeddings "
                "WHERE connection_id = :cid AND chunk_type = 'table'"
            ),
            {"cid": connection_id},
        )
    ).scalar_one()
    column_count = (
        await session.execute(
            text(
                "SELECT count(*) FROM pyrene_schema_embeddings "
                "WHERE connection_id = :cid AND chunk_type = 'column'"
            ),
            {"cid": connection_id},
        )
    ).scalar_one()
    return int(total), int(table_count), int(column_count)


def _git_sha() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            text=True,
        ).strip()
        return sha
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="3-way recall measurement for the schema-RAG retriever (PRD-043)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("measurement-result.md"),
        help="Output markdown path",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="Optional JSON output path (for CI artifact)",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(
            "packages/pyrene-sql/tests/data/schema_retrieval_30.yaml"
        ),
        help="Path to the 30-case yaml dataset",
    )
    parser.add_argument(
        "--connection-id",
        default=str(DEFAULT_CONNECTION_ID),
        help="connection_id UUID (Phase 1 default unless multi-tenant)",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    pg_dsn = os.environ.get("PG_DSN")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not pg_dsn:
        print("ERROR: PG_DSN env var required", file=sys.stderr)
        return 2
    if not openai_key:
        print("ERROR: OPENAI_API_KEY env var required", file=sys.stderr)
        return 2

    cases = load_dataset(args.dataset)
    embedder = OpenAIEmbedder(api_key=openai_key)
    connection_id = UUID(args.connection_id)

    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            total, table_count, column_count = await _index_chunk_summary(
                session, connection_id
            )
            if total == 0:
                print(
                    "ERROR: pyrene_schema_embeddings is empty — "
                    "run `pyrene-sql index-schema --reindex` first",
                    file=sys.stderr,
                )
                return 3

            results: list[VariantResult] = []
            for variant in VARIANTS:
                print(f"  measuring variant {variant.key} ({variant.label}) …")
                result = await measure_one_variant(
                    session=session,
                    embedder=embedder,
                    cases=cases,
                    variant=variant,
                    connection_id=connection_id,
                )
                print(
                    f"    top-3={result.top_3_accuracy:.1%}  "
                    f"top-5={result.top_5_accuracy:.1%}  "
                    f"p50={result.latency_p50_ms:.1f}ms"
                )
                results.append(result)
    finally:
        await engine.dispose()

    timestamp_iso = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    markdown = render_markdown_report(
        git_sha=_git_sha(),
        timestamp_iso=timestamp_iso,
        embedder_model=embedder._model,  # read-only attr lookup
        embedder_dimensions=EMBEDDING_DIMENSIONS,
        dataset_path=str(args.dataset),
        chunk_count=total,
        table_count=table_count,
        column_count=column_count,
        results=tuple(results),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"\nMarkdown report → {args.output}")

    if args.json_output is not None:
        payload = {
            "git_sha": _git_sha(),
            "timestamp_iso": timestamp_iso,
            "embedder_model": embedder._model,
            "embedder_dimensions": EMBEDDING_DIMENSIONS,
            "dataset_path": str(args.dataset),
            "chunks": {
                "total": total,
                "table": table_count,
                "column": column_count,
            },
            "variants": [
                {
                    "key": r.variant.key,
                    "label": r.variant.label,
                    "ef_search": r.variant.ef_search,
                    "chunk_type_filter": r.variant.chunk_type_filter,
                    "top_3_accuracy": r.top_3_accuracy,
                    "top_5_accuracy": r.top_5_accuracy,
                    "latency_p50_ms": r.latency_p50_ms,
                    "latency_p95_ms": r.latency_p95_ms,
                    "misses": [
                        {
                            "case_id": m.case_id,
                            "query": m.query,
                            "expected": list(m.expected),
                            "got_top_3": list(m.got_top_3),
                        }
                        for m in r.misses
                    ],
                }
                for r in results
            ],
        }
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"JSON report     → {args.json_output}")

    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
