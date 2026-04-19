#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/opt/dcss-n8n/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

echo "Backup retention policy: keep at least ${RETENTION_DAYS} days."
echo "Automatic deletion is intentionally disabled. Candidate files older than policy:"

find "$BACKUP_ROOT" -type f -mtime "+${RETENTION_DAYS}" -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' | sort
