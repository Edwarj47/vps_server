#!/usr/bin/env bash
set -euo pipefail

TRACKED="${TRACKED_CADDYFILE:-/opt/dcss-n8n/Caddy/Caddyfile}"
ACTIVE="${ACTIVE_CADDYFILE:-/etc/caddy/Caddyfile}"

tmp_tracked="$(mktemp)"
tmp_active="$(mktemp)"
cleanup() {
  rm -f "$tmp_tracked" "$tmp_active"
}
trap cleanup EXIT

caddy fmt "$TRACKED" > "$tmp_tracked"
sudo caddy fmt "$ACTIVE" > "$tmp_active"

if ! diff -u "$tmp_tracked" "$tmp_active"; then
  echo "Caddy drift detected between $TRACKED and $ACTIVE" >&2
  exit 1
fi

echo "Caddy config drift check OK"
