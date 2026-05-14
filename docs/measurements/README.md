# Recall measurements (PRD-043)

Schema-RAG retriever 의 *production OpenAI* 환경 측정 결과 모음.

## 파일 명명 규칙

```
YYYY-MM-DD-recall-baseline.md      # 정기/마일스톤 측정
YYYY-MM-DD-recall-<context>.md     # 특정 트리거의 측정 (예: prd-044-trigger)
YYYY-MM-DD-recall.json             # JSON dump (CI artifact)
```

## 측정 실행 방법

### A. 로컬

```bash
# 1. 사전 조건
docker compose up -d
docker compose exec pyrene-api uv run alembic upgrade head
docker compose exec pyrene-api uv run pyrene-sql index-schema --reindex

# 2. 측정 (OPENAI_API_KEY .env 또는 export)
OPENAI_API_KEY=$OPENAI_API_KEY \
PG_DSN="postgresql+asyncpg://pyrene:pyrene@localhost:5433/dvdrental" \
uv run python bin/measure_chunk_recall.py \
    --output docs/measurements/$(date +%Y-%m-%d)-recall-baseline.md \
    --json-output docs/measurements/$(date +%Y-%m-%d)-recall.json

# 3. 결과 commit (별도 docs PR 권장)
git add docs/measurements/
git commit -m "docs(PRD-043): $(date +%Y-%m-%d) recall baseline measurement"
```

### B. CI (workflow_dispatch)

1. GitHub Actions UI → **Measure Recall (manual)** workflow → **Run workflow**
2. 측정 완료 후 artifact (`recall-measurement-<run_id>`) 다운로드
3. 결과 markdown + json 을 `docs/measurements/` 에 commit (별도 docs PR)

`OPENAI_API_KEY` GitHub secret 사전 설정 필수 — Settings → Secrets and variables → Actions.

## 결과 해석 가이드

각 측정 markdown 의 *결론* 섹션은 자동 생성. 사람이 검토 후:

- **Hybrid 효과 (A vs C)**: ≥ +5%p → PRD-042 production 입증, ADR-021 후보
- **ef_search 영향 (A vs B)**: < 1%p → PRD-044 (ef_search 200 → 100) 안전
- **misses by variant**: 같은 query 가 *3 variant 모두 fail* → 데이터셋/임베더 자체 한계

## 측정 비용

- OpenAI text-embedding-3-small @ 1024-dim: $0.13 / 1M tokens
- 1회 측정: 30 query × ~10 tokens (query) + 인덱스 재구축 시 ~111 chunks × ~50 tokens ≈ **~6K tokens ≈ $0.0008**
- 인덱스 재사용 (재구축 안 함) 시: 30 query × 10 tokens ≈ **~300 tokens ≈ $0.00004**

→ 비용 무시 가능. 단 *cron 측정* 하면 누적 — 본 PRD 는 *workflow_dispatch only* 정책.

## 참고

- PRD-043 (`docs/prd/PRD-043-production-recall-measurement.md`) — 측정 인프라 PRD
- PLAN-043 (`docs/plan/PLAN-043-production-recall-measurement.md`)
- ADR-020 (`docs/adr/ADR-020-hybrid-chunk-strategy.md`) — Hybrid chunk strategy
- PRD-002 §6 — top-3 accuracy ≥ 90% hard threshold
