# DCSS n8n Docker

This folder provides a local Docker Desktop setup and a VPS setup using the same `n8n + postgres` stack, aligned with the current n8n-hosting compose example.

## Files
- `docker-compose.local.yml` for local Docker Desktop
- `docker-compose.vps.yml` for VPS deployment
- `.env.example` template for required secrets
- `.env.vps.example` VPS-specific template, including runner and Discord proxy settings
- `init-data.sh` for creating a non-root Postgres user
- `python-worker/` FastAPI Discord interactions proxy

## Local (Docker Desktop)
1) Copy `.env.example` to `.env` and fill values.
2) Run:

```bash
cd "$(pwd)"
docker compose -f docker-compose.local.yml --env-file .env up -d
```

## VPS (Hostinger)
1) Copy this folder to the server.
2) Copy `.env.vps.example` to `.env` and fill all `change-me` values.
3) Run:

```bash
docker compose -f docker-compose.vps.yml --env-file .env up -d
```

## Notes
- The containers are named `dcss-n8n` and `dcss-postgres`.
- Uses `docker.n8n.io/n8nio/n8n` and `postgres:16` per the current n8n-hosting compose example.
- The VPS compose file binds n8n and the Discord proxy to localhost. Place Caddy or another reverse proxy in front for TLS.
- If you want to pin a specific n8n version, replace `docker.n8n.io/n8nio/n8n` with a tagged version.
- Do not commit `.env`, `.env.*` backups, workflow exports, database dumps, or Docker volume data.
