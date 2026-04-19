#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-}"
STACK_DIR="${STACK_DIR:-/opt/dcss-n8n}"
ENV_FILE="${ENV_FILE:-${STACK_DIR}/Docker/.env}"
WORKFLOW_EXPORT="${WORKFLOW_EXPORT:-${STACK_DIR}/workflows/ollama-chat-webhook.json}"
WORKFLOW_ID="${WORKFLOW_ID:-4jxfmYpXsfhFO8Rc}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-dcss-postgres}"
POSTGRES_USER="${POSTGRES_USER:-n8n_dcss}"
POSTGRES_DB="${POSTGRES_DB:-n8n}"

if [[ -z "$MODEL" ]]; then
  if [[ -r "$ENV_FILE" ]]; then
    MODEL="$(sudo awk -F= '/^OLLAMA_CHAT_MODEL=/{print $2}' "$ENV_FILE" | tail -1)"
  fi
fi

if [[ -z "$MODEL" ]]; then
  echo "Usage: $0 <ollama-model-tag>" >&2
  exit 2
fi

if ! sudo docker exec dcss-ollama ollama list | awk 'NR > 1 {print $1}' | grep -Fxq "$MODEL"; then
  echo "Model '$MODEL' is not installed in Ollama. Pull it first, then rerun this script." >&2
  exit 3
fi

if sudo grep -q '^OLLAMA_CHAT_MODEL=' "$ENV_FILE"; then
  sudo perl -0pi -e "s/^OLLAMA_CHAT_MODEL=.*/OLLAMA_CHAT_MODEL=${MODEL}/m" "$ENV_FILE"
else
  printf '\nOLLAMA_CHAT_MODEL=%s\n' "$MODEL" | sudo tee -a "$ENV_FILE" >/dev/null
fi

tmp="$(mktemp)"
sudo jq --arg model "$MODEL" \
  '.[0].nodes |= map(
    if .name == "Ollama Chat Model" then
      (.parameters.model = $model)
    elif .name == "Parse Discord Interaction" then
      (.parameters.jsCode |= sub("const configuredModel = [^;]+;"; "const configuredModel = " + ($model|@json) + ";"))
    elif .name == "Build Chat Response" then
      (.parameters.jsCode |= sub("const modelUsed = String\\([^;]+\\);"; "const modelUsed = String(" + ($model|@json) + ");"))
    else
      .
    end
  )' \
  "$WORKFLOW_EXPORT" > "$tmp"
sudo install -o root -g root -m 0644 "$tmp" "$WORKFLOW_EXPORT"
rm -f "$tmp"

code="$(sudo jq -r '.[0].nodes[] | select(.name=="Build Chat Response") | .parameters.jsCode' "$WORKFLOW_EXPORT")"
parse_code="$(sudo jq -r '.[0].nodes[] | select(.name=="Parse Discord Interaction") | .parameters.jsCode' "$WORKFLOW_EXPORT")"
sudo docker exec -i "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v code="$code" -v parse_code="$parse_code" -v model="$MODEL" -v workflow_id="$WORKFLOW_ID" <<'SQL'
update workflow_entity
set nodes = (
  select jsonb_agg(
    case
      when elem->>'name' = 'Parse Discord Interaction'
        then jsonb_set(elem, '{parameters,jsCode}', to_jsonb(:'parse_code'::text))
      when elem->>'name' = 'Build Chat Response'
        then jsonb_set(elem, '{parameters,jsCode}', to_jsonb(:'code'::text))
      when elem->>'name' = 'Ollama Chat Model'
        then jsonb_set(elem, '{parameters,model}', to_jsonb(:'model'::text))
      else elem
    end
    order by ord
  )
  from jsonb_array_elements(nodes::jsonb) with ordinality as t(elem, ord)
)::json
where id = :'workflow_id';

update workflow_history
set nodes = (
  select jsonb_agg(
    case
      when elem->>'name' = 'Parse Discord Interaction'
        then jsonb_set(elem, '{parameters,jsCode}', to_jsonb(:'parse_code'::text))
      when elem->>'name' = 'Build Chat Response'
        then jsonb_set(elem, '{parameters,jsCode}', to_jsonb(:'code'::text))
      when elem->>'name' = 'Ollama Chat Model'
        then jsonb_set(elem, '{parameters,model}', to_jsonb(:'model'::text))
      else elem
    end
    order by ord
  )
  from jsonb_array_elements(nodes::jsonb) with ordinality as t(elem, ord)
)::json
where "workflowId" = :'workflow_id';
SQL

echo "OLLAMA_CHAT_MODEL set to $MODEL"
echo "Recreate n8n and python-worker, then verify /agent/status and /ask."
