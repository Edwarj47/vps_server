#!/usr/bin/env bash
set -euo pipefail

N8N_PUBLIC_HOST="${N8N_PUBLIC_HOST:-n8n.dcss.dev}"

check() {
  local name="$1"
  shift
  printf '%-34s' "$name"
  if "$@" >/tmp/dcss-health-check.out 2>/tmp/dcss-health-check.err; then
    printf 'OK\n'
  else
    printf 'FAIL\n'
    sed 's/^/  stdout: /' /tmp/dcss-health-check.out
    sed 's/^/  stderr: /' /tmp/dcss-health-check.err
    exit 1
  fi
}

check "container dcss-n8n" docker inspect -f '{{.State.Running}}' dcss-n8n
check "container dcss-postgres" docker inspect -f '{{.State.Health.Status}}' dcss-postgres
check "container dcss-ollama" docker inspect -f '{{.State.Running}}' dcss-ollama
check "container dcss-python-worker" docker inspect -f '{{.State.Running}}' dcss-python-worker
check "n8n local health" curl -fsS http://127.0.0.1:5678/healthz
check "n8n caddy health" curl -k -fsS "https://${N8N_PUBLIC_HOST}/healthz" --resolve "${N8N_PUBLIC_HOST}:443:127.0.0.1"
check "ollama from n8n network" docker exec dcss-n8n wget -qO- http://ollama:11434/api/tags
check "discord unsigned rejected" bash -c "status=\$(curl -k -sS -o /tmp/dcss-discord-check.out -w '%{http_code}' -X POST \"https://${N8N_PUBLIC_HOST}/discord/interactions\" --resolve \"${N8N_PUBLIC_HOST}:443:127.0.0.1\" -H 'Content-Type: application/json' --data '{}'); test \"\$status\" = 401"
check "n8n not public-bound" bash -c "! ss -tln | grep -Eq '(^|[[:space:]])(0\\.0\\.0\\.0|\\[::\\]):5678[[:space:]]'"

rm -f /tmp/dcss-health-check.out /tmp/dcss-health-check.err /tmp/dcss-discord-check.out
