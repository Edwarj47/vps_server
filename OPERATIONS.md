# DCSS n8n Stack Operations

## Layout

- Compose project: `/opt/dcss-n8n/Docker`
- Compose file: `/opt/dcss-n8n/Docker/docker-compose.vps.yml`
- Environment file: `/opt/dcss-n8n/Docker/.env`
- Local backups: `/opt/dcss-n8n/backups`
- n8n data volume: `docker_n8n_data`
- Postgres data volume: `docker_postgres_data`
- Ollama data volume: `docker_ollama_data`

## Public Routing

Caddy serves `https://n8n.dcss.dev`.

- `/discord/interactions` proxies to the local Python worker at `127.0.0.1:8001`.
- All other paths proxy to n8n at `127.0.0.1:5678`.

n8n should stay bound to localhost in Docker Compose. Do not publish it on `0.0.0.0`.

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

## Rollback for n8n Localhost Binding

Restore the pre-change compose backup, then recreate n8n:

```bash
sudo cp /opt/dcss-n8n/Docker/docker-compose.vps.yml.bak.20260417_021950.codex-pre-localbind /opt/dcss-n8n/Docker/docker-compose.vps.yml
sudo docker compose --env-file /opt/dcss-n8n/Docker/.env -f /opt/dcss-n8n/Docker/docker-compose.vps.yml up -d n8n
```
