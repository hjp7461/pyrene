# Pyrene

Self-hosted control planes for LLM agents are missing the basics — RBAC over tools and data, real-time cost ceilings, and an audit trail you can hand to security review. Pyrene is a Pydantic-AI-only, Postgres-native control plane that ships RBAC at the connection/database/schema/table layer, per-user budget gates, and structured tools (no raw SQL) as first-class primitives. This 12-week portfolio demonstrates three end-to-end scenarios — a permission denial with audit, a fail-closed budget exhaustion, and a multi-tool SQL→filesystem composition — backed by a Pydantic Evals suite and two public Logfire traces.

> 한국어 요약: 4계층 RBAC + 사용자 예산 + 구조화 도구. 시나리오 A 권한+감사, B 예산 fail-closed, C SQL→파일 + 공개 trace 2.

**Demo** (5 min, YouTube unlisted, _Day 2 placeholder_):

```bash
cp .env.example .env && docker compose up -d
bin/demo.sh all   # phase1 + A + B + C
```

Hook chain: `BUDGET_PRE=10 → TOOL_RBAC=20 → DATA_RBAC=30 → tool → AUDIT=80 → BUDGET_POST=90` on Postgres (ADR-013 dual-role). Streamlit shows audit/cost/RBAC.

**Traces** (_placeholder_): T1 Q1 spans — RBAC, cost $0.0021, N3 retry. T2 viewer `payment.amount` deny → audit → <50 ms.

**Signals**: 8 ADRs · 12 packages · 788 tests · mypy `--strict` 243 files · 42 security evals · WORM audit + hash chain · budget fail-closed.

**Not built**: row/col mask (ADR-007→v2), SSO. Phase 1: [`bin/demo-phase1.sh`](bin/demo-phase1.sh).
