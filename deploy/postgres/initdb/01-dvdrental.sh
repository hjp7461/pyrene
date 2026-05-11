#!/usr/bin/env bash
# Restore DVD Rental sample DB into ${POSTGRES_DB}.
# Source: https://neon.tech/postgresqltutorial/dvdrental.zip (mirror of postgresqltutorial.com).
set -euo pipefail

INITDB_DIR="$(dirname "$0")"
TAR_PATH="${INITDB_DIR}/dvdrental.tar"

if [[ ! -f "${TAR_PATH}" ]]; then
  echo "[01-dvdrental] dvdrental.tar not found at ${TAR_PATH}; skipping."
  exit 0
fi

echo "[01-dvdrental] restoring DVD Rental sample into '${POSTGRES_DB}' ..."
pg_restore \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --no-owner \
  --no-privileges \
  --exit-on-error \
  "${TAR_PATH}"

echo "[01-dvdrental] done."
