#!/usr/bin/env bash
set -euo pipefail

umask 077

BACKUP_ROOT="${BACKUP_ROOT:-/opt/dcss-n8n/backups}"
COMPOSE_FILE="${COMPOSE_FILE:-/opt/dcss-n8n/Docker/docker-compose.vps.yml}"
ENV_FILE="${ENV_FILE:-/opt/dcss-n8n/Docker/.env}"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"

POSTGRES_DIR="$BACKUP_ROOT/postgres"
WORKFLOWS_DIR="$BACKUP_ROOT/n8n-workflows"
CONFIG_DIR="$BACKUP_ROOT/config"
LOG_DIR="$BACKUP_ROOT/logs"

mkdir -p "$POSTGRES_DIR" "$WORKFLOWS_DIR" "$CONFIG_DIR" "$LOG_DIR"

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

require_container() {
  local name="$1"
  docker inspect "$name" >/dev/null 2>&1
}

log "starting backup $TIMESTAMP"

require_container dcss-postgres
require_container dcss-n8n

PG_TMP="/tmp/n8n-${TIMESTAMP}.dump"
PG_OUT="$POSTGRES_DIR/n8n-${TIMESTAMP}.dump"
log "exporting postgres database to $PG_OUT"
docker exec dcss-postgres pg_dump -U n8n -d n8n --format=custom --no-owner --file "$PG_TMP"
docker cp "dcss-postgres:$PG_TMP" "$PG_OUT"
docker exec dcss-postgres rm -f "$PG_TMP"
chmod 0600 "$PG_OUT"

WF_TMP="/tmp/n8n-workflows-${TIMESTAMP}.json"
WF_OUT="$WORKFLOWS_DIR/n8n-workflows-${TIMESTAMP}.json"
log "exporting n8n workflows to $WF_OUT"
docker exec dcss-n8n n8n export:workflow --all --pretty --output "$WF_TMP"
docker cp "dcss-n8n:$WF_TMP" "$WF_OUT"
docker exec dcss-n8n rm -f "$WF_TMP"
chmod 0600 "$WF_OUT"

CONFIG_OUT="$CONFIG_DIR/dcss-n8n-config-${TIMESTAMP}.tgz"
log "archiving stack config to $CONFIG_OUT"
tar -czf "$CONFIG_OUT" \
  -C /opt/dcss-n8n \
  Docker/docker-compose.vps.yml \
  Docker/.env \
  Docker/init-data.sh \
  Docker/python-worker/Dockerfile \
  Docker/python-worker/requirements.txt \
  Docker/python-worker/main.py \
  -C /etc \
  caddy/Caddyfile
chmod 0600 "$CONFIG_OUT"

MANIFEST="$BACKUP_ROOT/manifest-${TIMESTAMP}.sha256"
sha256sum "$PG_OUT" "$WF_OUT" "$CONFIG_OUT" > "$MANIFEST"
chmod 0600 "$MANIFEST"
log "wrote manifest $MANIFEST"

log "backup complete"
