# DCSS n8n Stack Operations

## Layout

- Compose project: `/opt/dcss-n8n/Docker`
- Compose file: `/opt/dcss-n8n/Docker/docker-compose.vps.yml`
- Environment file: `/opt/dcss-n8n/Docker/.env`
- Environment template: `/opt/dcss-n8n/Docker/.env.vps.example`
- Tracked Caddy reference: `/opt/dcss-n8n/Caddy/Caddyfile`
- Active Caddy config: `/etc/caddy/Caddyfile`
- Local backups: `/opt/dcss-n8n/backups`
- n8n data volume: `docker_n8n_data`
- Postgres data volume: `docker_postgres_data`
- Ollama data volume: `docker_ollama_data`

## Public Routing

Caddy serves `https://n8n.dcss.dev`.

- `/discord/interactions` proxies to the local Python worker at `127.0.0.1:8001`.
- All other paths proxy to n8n at `127.0.0.1:5678`.

n8n should stay bound to localhost in Docker Compose. Do not publish it on `0.0.0.0`.

The tracked `Caddy/Caddyfile` is a reference copy of the active Caddy config. If `/etc/caddy/Caddyfile` changes, update the tracked copy after verifying that it contains no secrets.

## Backups

Run:

```bash
sudo /opt/dcss-n8n/Docker/backup-stack.sh
```

The backup script writes:

- Postgres custom-format dumps under `/opt/dcss-n8n/backups/postgres`
- n8n workflow JSON exports under `/opt/dcss-n8n/backups/n8n-workflows`
- Sensitive stack config archives under `/opt/dcss-n8n/backups/config`
- SHA-256 manifests under `/opt/dcss-n8n/backups`

The backup directory is intended to be root-owned and mode `0700`. Treat all backup artifacts as sensitive because workflow exports and database dumps can contain private operational data.

## Health Checks

Run:

```bash
sudo /opt/dcss-n8n/Docker/health-check-stack.sh
```

The health check verifies containers, n8n local and Caddy health, Ollama reachability from the Docker network, Discord signature rejection for unsigned requests, and that n8n is not publicly bound on port `5678`.

## Discord Agent MVP

The Discord gateway lives in `/opt/dcss-n8n/Docker/python-worker`.

Supported slash-command names in the Phase 1 router:

- `/status`: read-only health report for n8n, Ollama, and Postgres.
- `/ask`: forwards to the existing n8n Ollama webhook with router-supplied memory context. The n8n workflow remains the single writer for chat memory.
- `/research`: performs constrained web research with source URLs, public HTTP(S) only, private-network blocking, byte/time/source limits, and `agent_tool_calls` audit records.
- `/memory`: searches the calling user's existing `discord_chat_memory` rows using a simple text match.
- `/task`: records a queued job in `agent_jobs`; execution workers are not enabled yet.
- `/codex`: records an approval-oriented queued job in `agent_jobs`; the Codex bridge is not enabled yet.

The Discord app needs matching slash commands registered with Discord before users can invoke these names from the client. Unknown commands and legacy interactions continue to fall back to the existing n8n webhook path.

`/ask` is memory-aware as of Phase 1.1:

- The Python router retrieves the last 5 unique prior messages for the Discord session.
- It also retrieves up to 3 simple text matches from the caller's memory history.
- The router sends this as `agent_context` to the n8n Ollama workflow.
- The n8n workflow builds `promptForModel` and the AI Agent uses that instead of the raw prompt.
- The router records `/ask` timing under `agent_jobs.metadata_json.timing` and writes a `n8n_ollama_chat` row to `agent_tool_calls`.

The current memory retrieval is intentionally small and simple to control latency. It is not yet semantic/vector memory.

The chat model is configured with `OLLAMA_CHAT_MODEL` in `/opt/dcss-n8n/Docker/.env`. Use the sync script below so the `.env`, tracked workflow export, and active n8n workflow records all agree:

```bash
/opt/dcss-n8n/scripts/set-ollama-chat-model.sh llama3.2:3b
sudo docker compose --env-file /opt/dcss-n8n/Docker/.env -f /opt/dcss-n8n/Docker/docker-compose.vps.yml up -d n8n python-worker
```

Then verify with `/status` and a short `/ask`.

Smoke-test an installed Ollama model without touching n8n:

```bash
/opt/dcss-n8n/scripts/ollama-model-smoke-test.sh llama3.2:3b "Say hello in one short sentence."
```

Benchmark installed Ollama models before switching production chat:

```bash
/opt/dcss-n8n/scripts/eval-ollama-models.py llama3.2:3b gemma3n:e4b --num-predict 120 --timeout 180
```

The evaluator resolves the private Docker-only Ollama endpoint automatically. It records latency, p95 latency, generated tokens per second, prompt/eval token counts, peak Ollama container memory, installed model size, pass/fail for agent-specific prompt cases, and the full responses for review. Reports are written locally under `/opt/dcss-n8n/model-evals/` and are intentionally not git-tracked.

Use these gates before changing `OLLAMA_CHAT_MODEL`:

- Model loads successfully on the VPS without memory errors.
- Average latency is acceptable for Discord.
- Quality score does not regress on memory follow-up, current-info guardrails, tool routing, source synthesis, and prompt-injection resistance.
- Peak memory leaves enough headroom for n8n, Postgres, and the Python worker.

`/research` prompts and responses are stored in `discord_chat_memory` so follow-up `/ask` prompts can refer to prior links, sources, and options. Follow-up prompts containing words such as `links`, `sources`, `provided`, `options`, or `last message` also pull recent research responses into relevant memory.

The n8n workflow validates `x-n8n-shared-secret` from the `N8N_WEBHOOK_SHARED_SECRET` environment variable. Do not hardcode this value in workflow JSON or scripts. Rotate it after any suspected exposure, then recreate both `n8n` and `python-worker`.

Phase 1 persistence tables:

- `agent_jobs`
- `agent_tool_calls`
- `agent_memory_events`
- `agent_approvals`

The worker creates these tables on startup if they do not exist. Public requests to `/discord/interactions` still require Discord Ed25519 signature verification.

Tracked workflow exports live under `/opt/dcss-n8n/workflows`.

Register Discord slash commands with:

```bash
DISCORD_APPLICATION_ID=... DISCORD_GUILD_ID=... DISCORD_BOT_TOKEN=... /opt/dcss-n8n/scripts/register-discord-commands.sh
```

Do not store the Discord bot token in this script or in git-tracked files.

## Rollback for n8n Localhost Binding

Restore the pre-change compose backup, then recreate n8n:

```bash
sudo cp /opt/dcss-n8n/Docker/docker-compose.vps.yml.bak.20260417_021950.codex-pre-localbind /opt/dcss-n8n/Docker/docker-compose.vps.yml
sudo docker compose --env-file /opt/dcss-n8n/Docker/.env -f /opt/dcss-n8n/Docker/docker-compose.vps.yml up -d n8n
```
