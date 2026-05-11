# Pyrene

**Pydantic AI 전용 · 셀프호스트 우선 · RBAC 1급** — LLM 에이전트 컨트롤 플레인.

LangSmith / Portkey / Helicone / Langfuse 카테고리에서, *셀프호스트로 돌릴 수 있고 RBAC을 1급 시민으로 다루는* 컨트롤 플레인은 비어 있다. Pyrene은 그 자리를 채운다 — Pydantic AI ≥1.93 / PostgreSQL 16 + pgvector 위에서 **4계층 RBAC** (Connection → Database → Schema → Table), **사용자/팀 단위 예산 fail-closed**, **WORM 감사 + per-team hash chain**, **구조화 도구 (raw SQL 금지)** 를 기본값으로 제공한다.

> 한 줄 요약: 구조화 도구 + 4계층 RBAC + fail-closed 예산 + WORM 감사 — 모두 단일 Hook chain 위에서 결정성·관측성을 잃지 않는다.

---

## 핵심 시그널

| 항목 | 값 |
|------|---|
| 워크스페이스 패키지 | **12** (`packages/*`) |
| 자동화 테스트 | **818** (`pytest --collect-only` · 815 active + 3 live-marker skipped) |
| 타입 안전 | `mypy --strict` 통과 (워크스페이스 전체 246 source files) |
| 마이그레이션 | Alembic **0001 → 0008** (단일 chain) |
| 아키텍처 결정 기록 (ADR) | **9건** (Pydantic AI 통합 · 예산 fail-closed · Postgres 운영정책 · 테스트 격리 · LLM retry boundary 등) |
| 시나리오 | Phase 1 (Q1/Q2/Q3/F1/F2) + Phase 2 (A: RBAC 거부 · B: 예산 거부 · C: SQL→파일 합성) |
| 관측성 | Logfire (선택), OTel 호환 span — `LOGFIRE_TOKEN` 설정 시 활성화 |
| 보안 evals | 42건 (CI: `.github/workflows/security-evals.yml`) |
| 데모 결정성 | `.env`만 채우면 `bin/demo-phase1.sh` 4/4 PASS (셸 export 불필요, PRD-019) |

---

## 아키텍처

### Hook chain (Gateway 등록 순서)

```
요청
  │
  ▼
BUDGET_PRE  =10   ← 예산 pre-flight (advisory lock + 한도 검증, fail-closed)
  │
TOOL_RBAC   =20   ← 도구 단위 RBAC (deny precedence)
  │
DATA_RBAC   =30   ← 4계층 데이터 RBAC (Connection/DB/Schema/Table + wildcard)
  │
  ▼  ── 도구 실행 ──
  │
COST        =75   ← 토큰 사용량 × 단가 → usage_records
  │
AUDIT       =80   ← WORM 감사 (per-team hash chain, 위변조 검출)
  │
BUDGET_POST =90   ← 실제 비용 vs 한도 재검증
  │
  ▼
응답
```

40 ~ 70 우선순위 구간은 향후 hook 예약 영역이다.

### 12 패키지

| 패키지 | 역할 |
|--------|------|
| `pyrene-core` | `StrictBaseModel` · errors · audit Protocol · Logfire 설정 (의존성 0) |
| `pyrene-sql` | Phase 1 SQL analyst — 구조화 도구 (`run_select` / `run_join` / `run_aggregate`) · 스키마 RAG · 외부 retry · evals |
| `pyrene-auth` | User / Team / Role / UserTeamRole · 비밀번호 hashing (argon2) · JWT |
| `pyrene-agents` | AgentSpec registry · agent builder · ToolRegistry |
| `pyrene-gateway` | Hook chain runtime · AuditSink Protocol · MCP gateway |
| `pyrene-rbac` | 도구 단위 RBAC (Permission · PermissionResolver · deny precedence) |
| `pyrene-data-rbac` | 4계층 데이터 RBAC (F-08) — Connection/DB/Schema/Table + wildcard |
| `pyrene-metering` | UsageRecord · 모델별 단가 · 토큰 → 비용 환산 |
| `pyrene-audit` | WORM 트리거 + per-team hash chain · 무결성 검증 |
| `pyrene-budget` | Limit (일/월) · advisory lock · pre/post hook fail-closed (ADR-010) |
| `pyrene-mcp-tools` | Filesystem (O_NOFOLLOW + sandbox root) · GitHub MCP 래퍼 |
| `pyrene-dashboard` | Streamlit 어드민 (RBAC matrix · 거부 카운터 · 예산 heatmap · 비용 · 감사) |

**의존 규칙**: `pyrene-core`만 다른 패키지 의존 0. 신규 도메인 패키지는 `pyrene-core` + `pyrene-auth` + `pyrene-gateway`까지만 의존 허용 (cross-import 금지).

---

## 빠른 시작

### 사전 요구사항

- **Docker** + **Docker Compose v2** (Postgres 16 + pgvector 이미지를 받기 위해)
- **Python 3.13+** 와 **[uv](https://docs.astral.sh/uv/)** (로컬 개발/테스트 시)
- (선택) **`ANTHROPIC_API_KEY`** — Phase 1 데모에서 실 LLM 호출을 하려면 필요
- (선택) **`OPENAI_API_KEY`** — 스키마 RAG 인덱싱 (`pyrene-sql index-schema`) 시 필요
- (선택) **`LOGFIRE_TOKEN`** — 분산 trace 가시화

### 1. 환경 변수 준비

```bash
cp .env.example .env
# .env 를 열어 ANTHROPIC_API_KEY, OPENAI_API_KEY, LOGFIRE_TOKEN(선택), JWT_SECRET 등을 채운다.
```

> `JWT_SECRET`은 로컬 기본값(`pyrene-dev-secret-...`)이 들어있지만, 외부에 노출되는 환경에서는 **반드시 32바이트 이상의 고엔트로피 값으로 재정의**해야 한다.

### 2. Docker로 풀스택 기동

가장 빠르게 모든 컴포넌트를 띄우는 방법:

```bash
# 기본: postgres + Streamlit 대시보드만 (가벼움)
docker compose up -d

# 풀스택: + 통합 FastAPI (auth/agents/gateway/rbac/data-rbac/metering/audit/budget) + echo MCP
docker compose --profile api up -d
```

기동 후 접속:

| 서비스 | URL | 비고 |
|--------|-----|------|
| Streamlit 대시보드 | http://localhost:8501 | RBAC 매트릭스 / 거부 카운터 / 예산 heatmap / 비용 / 감사 |
| 통합 API (`--profile api`) | http://localhost:8000 | `/health`, `/auth/*`, `/agents/*`, `/servers/*`, `/permissions/*`, … |
| Postgres | `localhost:5433` | user=`pyrene`, db=`dvdrental` (DVD Rental 샘플 + 마이그레이션 자동 적용) |
| echo-mcp (`--profile api`) | http://localhost:9000 | gateway 통합 테스트용 stub MCP |

Postgres 컨테이너는 기동 시 `deploy/postgres/initdb/`의 시드 스크립트로 **DVD Rental 샘플 + read-only 역할 (`pyrene_readonly`)** 까지 자동 설정한다 (ADR-013 dual-role).

### 3. 로컬 개발 (uv workspace)

컨테이너 없이 워크스페이스에서 직접 작업할 때:

```bash
uv sync                              # 의존성 설치 (12 패키지 + dev group)
uv run alembic upgrade head          # 마이그레이션 적용 (Postgres가 5433에 떠 있다고 가정)
uv run pytest packages -q            # 전체 테스트 (818개)
uv run mypy --strict packages        # 타입 체크
uv run ruff check && uv run ruff format --check    # 린트/포맷
```

### 4. 데모 실행

`bin/demo.sh`는 모든 시나리오의 단일 진입점이다.

```bash
# 전체 (Phase 1 → A → B → C → 대시보드 URL, 약 5분 분량)
bin/demo.sh all

# 부분
bin/demo.sh phase1       # Phase 1: Q1 / Q2 / Q3 (실 LLM 호출, ANTHROPIC_API_KEY 필요)
bin/demo.sh scenario-a   # RBAC 거부 + WORM 감사
bin/demo.sh scenario-b   # 예산 fail-closed (BUDGET_PRE=10 hook)
bin/demo.sh scenario-c   # SQL → 마크다운 → Filesystem MCP 합성
bin/demo.sh dashboard    # Streamlit URL 안내
```

유용한 환경 변수:

| 변수 | 효과 |
|------|------|
| `PYRENE_DEMO_SKIP_LLM=1` | Phase 1 LLM 호출 스킵 (스택 헬스 체크만) — CI 친화 |
| `PYRENE_DEMO_VERBOSE=1` | `pyrene-sql ask` 출력을 raw JSON으로 |
| `PYRENE_DEMO_NO_COMPOSE=1` | `docker compose up` 스킵 (이미 떠 있다고 가정) |

> **참고**: Phase 1 (Q1/Q2/Q3)은 실제 Pydantic AI 에이전트를 호출한다. 시나리오 A/B/C는 hook chain의 *순서와 결과를 관측 가능한 형태로 narrate* 하는 스크립트다 — 동일한 흐름의 풀 HTTP roundtrip은 `pytest packages/pyrene-gateway/tests/integration -m integration`이 검증한다.

### 5. 첫 어드민 계정 생성 (선택)

대시보드/API를 실제로 로그인해서 둘러보고 싶다면:

```bash
# .env 의 ADMIN_EMAIL / ADMIN_PASSWORD 가 사용된다 (또는 플래그)
uv run pyrene-auth init-admin
# 또는: uv run pyrene-auth init-admin --email me@example.com --password '...'
```

이 커맨드는 idempotent — 같은 이메일로 다시 호출하면 비밀번호만 갱신된다.

---

## CLI 요약

| 커맨드 | 패키지 | 설명 |
|--------|--------|------|
| `pyrene-sql ask <질문>` | `pyrene-sql` | DVD Rental에 자연어 질의 → 구조화 도구 호출 → `AnalystResponse` |
| `pyrene-sql index-schema` | `pyrene-sql` | pgvector에 스키마 카드 인덱싱 (RAG, F-05) |
| `pyrene-auth init-admin` | `pyrene-auth` | 첫 admin (team=default, role=admin) 부트스트랩 |
| `pyrene-agents …` | `pyrene-agents` | AgentSpec CRUD / run (subcommand 다수) |
| `pyrene-dashboard` | `pyrene-dashboard` | Streamlit 진입점 (compose가 이미 띄움) |

---

## 테스트 / 검증

```bash
uv run pytest packages -q                                        # 전체 (818건 — 815 active + 3 live skip)
uv run pytest packages/pyrene-sql/tests/evals -q                 # Pydantic Evals 데이터셋
uv run pytest packages -m integration -q                         # testcontainers Postgres 통합
uv run pytest packages/pyrene-budget/tests/evals -q              # 보안 evals — 예산 우회 / advisory lock
```

CI 파이프라인 (`.github/workflows/`):

- `ci.yml` — 린트 (ruff) + 타입 (mypy --strict) + 단위/통합 테스트
- `evals.yml` — Pydantic Evals 회귀 (모델 안전성 / 정확도)
- `security-evals.yml` — RBAC 우회 / 예산 우회 / WORM 무결성 42건

---

## 시나리오 (Phase 2)

| 시나리오 | 핵심 검증 |
|----------|----------|
| **A. RBAC 거부 + 감사** | `analyst` 는 `public.payment.amount` SELECT 통과, `viewer`는 403 `RBACDenied` — 동일 요청이 WORM 감사 1행으로 기록되고 per-team hash chain의 `prev_hash` 가 일치 (위변조 검출 가능) |
| **B. 예산 fail-closed** | `viewer` 의 일일 한도 \$1.00 도달 직후 다음 요청이 `BUDGET_PRE=10` hook에서 402 `BudgetExhausted` 로 차단 — **도구 실행 0, 비용 발생 0** (ADR-010) |
| **C. 다중 도구 합성** | `analyst` 가 "카테고리별 매출 Top 5 → /tmp/report.md" 요청 → Postgres MCP `run_aggregate` → markdown 생성 → `FilesystemMcpTool.write` (`O_NOFOLLOW` + sandbox root) — 단일 trace에 2개의 tool span, cost/audit/RBAC 전체 커버 |

---

## 고정 결정 (요약, 13건)

| # | 결정 |
|---|------|
| F-01 | 모노레포 + uv workspace |
| F-02 | 도구는 raw SQL이 아닌 **구조화된 형태** (`run_select(table=, columns=, …)`) — RBAC을 string match로 처리 |
| F-03 | **코드 가드 + DB 가드 이중 방어** (read-only role) |
| F-04 | Self-correction 최대 **3회 재시도** (빈 결과/타임아웃/권한 거부는 재시도 X) |
| F-05 | 스키마 인지: **pgvector RAG** (전체 스키마 주입 X) |
| F-06 | 출력: SQL + 자연어 설명 + 결과 + 분석 + confidence |
| F-07 | 데모 DB: PostgreSQL DVD Rental |
| F-08 | RBAC 4계층: **Connection → Database → Schema → Table** (행/컬럼 마스킹 제외) |
| F-09 | 메트링 1순위: **비용** (토큰/모델별 \$, 예산, 알람) |
| F-10 | 배포: **docker-compose만** (k8s/AWS 미사용) |
| F-11 | UI 최소화: **Streamlit** (React 미사용) |
| F-12 | 관측성: **Logfire 필수 (선택)** + OTel 호환 |
| F-13 | 면접 시그널 3가지: **ADR · Pydantic Evals · 공개 Logfire trace** |

새 결정은 ADR로 기록한 뒤에만 이 표를 갱신한다.

---

## 의도적 미구현 (v2 이월)

- **행/컬럼 마스킹** — SQL 파서 직접 구현은 시간 예산 함정 (ADR-007 v2 deferral)
- **SSO / OIDC** — JWT만 제공 (외곽에서 Reverse proxy로 연동하는 것을 가정)
- **k8s / AWS 배포 manifest** — docker-compose 단일 진입점 (F-10)
- **React 풀스택 UI** — Streamlit으로 충분 (F-11). 이 포트폴리오의 정체성이 아님

---

## 라이선스

[MIT](./LICENSE)
