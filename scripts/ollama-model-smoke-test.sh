#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-${OLLAMA_CHAT_MODEL:-llama3.2:3b}}"
PROMPT="${2:-Reply with one short sentence explaining what you are.}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-dcss-ollama}"
NUM_PREDICT="${NUM_PREDICT:-48}"

container_ip="$(sudo docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$OLLAMA_CONTAINER")"
if [[ -z "$container_ip" ]]; then
  echo "Could not resolve Ollama container IP for $OLLAMA_CONTAINER" >&2
  exit 1
fi

start_ms="$(date +%s%3N)"
response="$(
  MODEL="$MODEL" PROMPT="$PROMPT" NUM_PREDICT="$NUM_PREDICT" OLLAMA_URL="http://${container_ip}:11434/api/generate" python3 - <<'PY'
import json
import os
import sys
import urllib.request

payload = {
    "model": os.environ["MODEL"],
    "prompt": os.environ["PROMPT"],
    "stream": False,
    "options": {"num_predict": int(os.environ["NUM_PREDICT"])},
}
req = urllib.request.Request(
    os.environ["OLLAMA_URL"],
    data=json.dumps(payload).encode("utf-8"),
    headers={"content-type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
except Exception as exc:
    print(f"request_error={type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)

print(data.get("response", "").strip().replace("\n", " "))
PY
)"
end_ms="$(date +%s%3N)"

printf 'model=%s\n' "$MODEL"
printf 'elapsed_ms=%s\n' "$((end_ms - start_ms))"
printf 'response=%s\n' "$(printf '%s' "$response" | sed -E 's/[[:space:]]+/ /g' | cut -c1-600)"
