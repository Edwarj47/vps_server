#!/usr/bin/env bash
  set -euo pipefail

  cd /opt/dcss-n8n/Docker
  ENV_FILE=".env"
  COMPOSE_FILE="docker-compose.vps.yml"

  if ! grep -q "task-runners" "$COMPOSE_FILE"; then
    echo "ERROR: $COMPOSE_FILE missing task-runners."
    exit 1
  fi

  cp "$ENV_FILE" "${ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"

  upsert() {
    key="$1"
    value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
      sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
      echo "${key}=${value}" >> "$ENV_FILE"
    fi
  }

  if grep -q '^N8N_RUNNERS_AUTH_TOKEN=' "$ENV_FILE"; then
    RUNNER_TOKEN="$(grep '^N8N_RUNNERS_AUTH_TOKEN=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
  else
    RUNNER_TOKEN="$(openssl rand -hex 32)"
  fi

  upsert N8N_IMAGE_TAG 2.7.4
  upsert N8N_RUNNERS_ENABLED true
  upsert N8N_RUNNERS_MODE external
  upsert N8N_RUNNERS_AUTH_TOKEN "$RUNNER_TOKEN"
  upsert N8N_NATIVE_PYTHON_RUNNER true

  echo "Updated values:"

  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps
  docker logs dcss-n8n-runners --since 10m | tail -n 120

  echo "Done."
