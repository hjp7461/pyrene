#!/usr/bin/env bash
# bin/check.sh — CI vs 로컬 표준 명령 단일 진입점 (PRD-027).
#
# CI(.github/workflows/ci.yml) 의 invocation 과 1:1 매칭. commit 전 / PR 등록
# 전 *3 단계 순차 검증*. 한 단계 실패 시 즉시 종료 (fail-fast).
#
# 참고: .claude/rules/operational-notes.md §"CI vs 로컬 정적 검증 일치"

set -euo pipefail

echo "==> 1/3  Lint (ruff check)"
uv run ruff check

echo "==> 2/3  Type check (mypy --strict packages)"
uv run mypy --strict packages

echo "==> 3/3  Test (pytest packages -q)"
uv run pytest packages -q

echo
echo "✓ 3 단계 모두 통과 — CI 와 동등 invariant 만족."
