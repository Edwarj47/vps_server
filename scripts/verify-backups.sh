#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/opt/dcss-n8n/backups}"
MODE="${1:---latest}"

if [[ "$MODE" != "--latest" && "$MODE" != "--all" ]]; then
  echo "Usage: $0 [--latest|--all]" >&2
  exit 2
fi

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

latest_manifest() {
  find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'manifest-*.sha256' -printf '%T@ %p\n' \
    | sort -nr \
    | awk 'NR == 1 {print $2}'
}

if [[ "$MODE" == "--latest" ]]; then
  manifest="$(latest_manifest)"
  if [[ -z "${manifest:-}" ]]; then
    echo "No backup manifests found under $BACKUP_ROOT" >&2
    exit 1
  fi
  manifests=("$manifest")
else
  mapfile -t manifests < <(find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'manifest-*.sha256' | sort)
fi

if [[ "${#manifests[@]}" -eq 0 ]]; then
  echo "No backup manifests found under $BACKUP_ROOT" >&2
  exit 1
fi

for manifest in "${manifests[@]}"; do
  log "verifying manifest $manifest"
  (cd / && sha256sum -c "$manifest")

  timestamp="$(basename "$manifest" | sed -E 's/^manifest-(.*)\.sha256$/\1/')"
  pg_dump="$BACKUP_ROOT/postgres/n8n-${timestamp}.dump"
  workflow_json="$BACKUP_ROOT/n8n-workflows/n8n-workflows-${timestamp}.json"
  config_tgz="$BACKUP_ROOT/config/dcss-n8n-config-${timestamp}.tgz"

  if [[ -f "$pg_dump" ]]; then
    log "checking postgres dump catalog $pg_dump"
    docker exec -i dcss-postgres pg_restore -l < "$pg_dump" >/dev/null
  fi

  if [[ -f "$workflow_json" ]]; then
    log "checking workflow JSON $workflow_json"
    jq type "$workflow_json" >/dev/null
  fi

  if [[ -f "$config_tgz" ]]; then
    log "checking config archive $config_tgz"
    tar -tzf "$config_tgz" >/dev/null
  fi
done

log "backup verification complete"
