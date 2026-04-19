#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/opt/dcss-n8n/backups}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-dcss-postgres}"
POSTGRES_USER="${POSTGRES_USER:-n8n}"
MODE="${1:---latest}"

if [[ "$MODE" != "--latest" ]]; then
  echo "Usage: $0 --latest" >&2
  exit 2
fi

latest_dump="$(
  find "$BACKUP_ROOT/postgres" -maxdepth 1 -type f -name 'n8n-*.dump' -printf '%T@ %p\n' \
    | sort -nr \
    | awk 'NR == 1 {print $2}'
)"

if [[ -z "${latest_dump:-}" ]]; then
  echo "No Postgres backup dumps found under $BACKUP_ROOT/postgres" >&2
  exit 1
fi

restore_db="dcss_restore_check_$(date -u +%Y%m%d_%H%M%S)"

cleanup() {
  docker exec "$POSTGRES_CONTAINER" dropdb -U "$POSTGRES_USER" --if-exists "$restore_db" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Creating temporary restore database $restore_db"
docker exec "$POSTGRES_CONTAINER" createdb -U "$POSTGRES_USER" "$restore_db"

echo "Restoring $latest_dump into $restore_db"
docker exec -i "$POSTGRES_CONTAINER" pg_restore -U "$POSTGRES_USER" -d "$restore_db" --no-owner --exit-on-error < "$latest_dump"

echo "Checking restored database table count"
docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$restore_db" -Atc \
  "select count(*) from information_schema.tables where table_schema = 'public';"

echo "Postgres restore test complete; temporary database will be dropped"
