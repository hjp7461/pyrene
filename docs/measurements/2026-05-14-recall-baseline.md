# Recall measurement — 2026-05-14 15:24:15 UTC

| | |
|---|---|
| git SHA | `cb83684` |
| OpenAI model | text-embedding-3-small @ 1024-dim |
| dataset | `packages/pyrene-sql/tests/data/schema_retrieval_30.yaml` (30 cases) |
| index | 30 BASE TABLES + 188 columns = 218 chunks |

## Accuracy

| Variant | top-3 | top-5 | latency_p50 | latency_p95 |
|---------|-------|-------|-------------|-------------|
| **A.** Hybrid + ef_search=200 | 100.0% | 100.0% | 4.6ms | 6.1ms |
| **B.** Hybrid + ef_search=100 | 100.0% | 100.0% | 4.5ms | 5.7ms |
| **C.** Pure (table-only) + ef_search=200 | 100.0% | 100.0% | 4.6ms | 6.7ms |

## Variant descriptions

- **A.** Hybrid + ef_search=200 — 현재 production state (PRD-042 + PRD-041)
- **B.** Hybrid + ef_search=100 — PRD-041 원복 시뮬 (PRD-044 OQ-7 시드)
- **C.** Pure (table-only) + ef_search=200 — Hybrid 효과 비교 baseline (PRD-042 효과 입증)

## Misses by variant

### A. Hybrid + ef_search=200

- (no misses — 100% top-3 accuracy)

### B. Hybrid + ef_search=100

- (no misses — 100% top-3 accuracy)

### C. Pure (table-only) + ef_search=200

- (no misses — 100% top-3 accuracy)

## 결론 (사용자 검토 후 수정)

- **Hybrid 효과**: A (100.0%) vs C (100.0%) → **+0.0%p**
- **ef_search 영향**: A (100.0%, ef=200) vs B (100.0%, ef=100) → **+0.0%p**
- **PRD-044 권장**: ef_search 200 → 100 원복 안전 (Δ < 1%p)
