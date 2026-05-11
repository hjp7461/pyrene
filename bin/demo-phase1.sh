#!/usr/bin/env bash
# PLAN-001 Day 3 — Phase 1 end-to-end demo.
#
# Brings up the dockerised DVD Rental Postgres, waits for the healthcheck, then
# runs the four PRD-001 scenarios (S1, S2, F1, F2) end-to-end against the live
# Pydantic AI agent. Each scenario's outcome is reported; the script continues
# on individual scenario failure and reports the final pass count at exit.
#
# Env knobs:
#   PYRENE_DEMO_VERBOSE=1  → print raw JSON output (default uses --pretty).
#   PYRENE_DEMO_SKIP_LLM=1 → skip the four ask calls (still verifies that the
#                            stack is up). Useful in CI without an API key.
#
# Exit codes:
#   0 — all attempted scenarios passed.
#   1 — at least one scenario failed (or the stack did not come up).

set -euo pipefail

# ---------- pretty output helpers -------------------------------------------
if [ -t 1 ]; then
  C_RESET="\033[0m"
  C_BOLD="\033[1m"
  C_DIM="\033[2m"
  C_GREEN="\033[32m"
  C_YELLOW="\033[33m"
  C_RED="\033[31m"
  C_CYAN="\033[36m"
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""
fi

log()  { printf "%b\n" "${C_CYAN}${C_BOLD}[demo]${C_RESET} $*"; }
ok()   { printf "%b\n" "${C_GREEN}${C_BOLD}[ok]${C_RESET}   $*"; }
warn() { printf "%b\n" "${C_YELLOW}${C_BOLD}[warn]${C_RESET} $*"; }
fail() { printf "%b\n" "${C_RED}${C_BOLD}[fail]${C_RESET} $*"; }
hr()   { printf "%b\n" "${C_DIM}--------------------------------------------------------------------${C_RESET}"; }

# ---------- locate repo root -------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# ---------- pick docker compose flavour --------------------------------------
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  fail "neither 'docker compose' nor 'docker-compose' is available on PATH"
  exit 1
fi

# ---------- bring up postgres ------------------------------------------------
log "starting postgres container via ${DC[*]} up -d ..."
"${DC[@]}" up -d

# ---------- wait for healthcheck --------------------------------------------
# We poll docker for the postgres container's health status. The compose file
# sets a healthcheck (pg_isready) so this is authoritative.
log "waiting for postgres healthcheck (max 90s, includes DVD Rental restore) ..."
SECONDS_WAITED=0
HEALTHY="no"
while [ "${SECONDS_WAITED}" -lt 90 ]; do
  STATUS="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' pyrene-postgres 2>/dev/null || echo missing)"
  case "${STATUS}" in
    healthy)
      HEALTHY="yes"
      break
      ;;
    unhealthy)
      fail "postgres reported unhealthy — inspect: docker logs pyrene-postgres"
      exit 1
      ;;
  esac
  sleep 2
  SECONDS_WAITED=$((SECONDS_WAITED + 2))
done
if [ "${HEALTHY}" != "yes" ]; then
  fail "postgres did not become healthy within 90s (status=${STATUS})"
  exit 1
fi
ok "postgres is healthy after ~${SECONDS_WAITED}s"

# Small additional pause to let initdb finish if it just completed.
sleep 1

# ---------- choose pyrene-sql invocation -------------------------------------
# Prefer the installed entrypoint; fall back to `uv run` for dev usage.
if command -v pyrene-sql >/dev/null 2>&1; then
  PYRENE=(pyrene-sql)
elif command -v uv >/dev/null 2>&1; then
  PYRENE=(uv run pyrene-sql)
else
  fail "neither 'pyrene-sql' nor 'uv' is on PATH — install the package first"
  exit 1
fi

# ---------- run scenarios ----------------------------------------------------
PYRENE_DEMO_VERBOSE="${PYRENE_DEMO_VERBOSE:-0}"
PYRENE_DEMO_SKIP_LLM="${PYRENE_DEMO_SKIP_LLM:-0}"

if [ "${PYRENE_DEMO_SKIP_LLM}" = "1" ]; then
  warn "PYRENE_DEMO_SKIP_LLM=1 — skipping the four ask scenarios (stack-only check)"
  ok "Phase 1 demo complete (skip-llm mode)"
  exit 0
fi

PASS=0
FAIL=0

run_scenario() {
  local label="$1"
  local question="$2"
  hr
  log "${C_BOLD}${label}${C_RESET} → ${question}"
  hr
  local args=(ask "${question}")
  if [ "${PYRENE_DEMO_VERBOSE}" != "1" ]; then
    args+=(--pretty)
  fi
  if "${PYRENE[@]}" "${args[@]}"; then
    ok "${label} passed"
    PASS=$((PASS + 1))
  else
    fail "${label} did not complete cleanly"
    FAIL=$((FAIL + 1))
  fi
  sleep 2
}

run_scenario "S1 simple SELECT"        "List 5 film categories"
run_scenario "S2 SELECT with filter"   "How many customers signed up in 2007?"
run_scenario "F1 write request refused" "Delete the customer whose id is 1"
run_scenario "F2 out-of-domain question" "What's the meaning of life?"

hr
log "results: ${C_GREEN}${PASS} passed${C_RESET}, ${C_RED}${FAIL} failed${C_RESET}"
if [ "${FAIL}" -eq 0 ]; then
  ok "Phase 1 demo complete"
  exit 0
fi
fail "Phase 1 demo finished with ${FAIL} scenario failure(s)"
exit 1
