#!/usr/bin/env bash
set -euo pipefail

DISK_PATH="${DISK_PATH:-/}"
DISK_WARN_PCT="${DISK_WARN_PCT:-80}"
DISK_CRIT_PCT="${DISK_CRIT_PCT:-90}"
MEM_AVAILABLE_WARN_PCT="${MEM_AVAILABLE_WARN_PCT:-20}"
MEM_AVAILABLE_CRIT_PCT="${MEM_AVAILABLE_CRIT_PCT:-10}"

disk_used_pct="$(df -P "$DISK_PATH" | awk 'NR == 2 {gsub(/%/, "", $5); print $5}')"
mem_total_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
mem_available_kb="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
mem_available_pct="$(( mem_available_kb * 100 / mem_total_kb ))"

status="OK"

if (( disk_used_pct >= DISK_CRIT_PCT )); then
  echo "CRITICAL: disk usage on $DISK_PATH is ${disk_used_pct}% >= ${DISK_CRIT_PCT}%" >&2
  status="CRITICAL"
elif (( disk_used_pct >= DISK_WARN_PCT )); then
  echo "WARNING: disk usage on $DISK_PATH is ${disk_used_pct}% >= ${DISK_WARN_PCT}%" >&2
  status="WARNING"
fi

if (( mem_available_pct <= MEM_AVAILABLE_CRIT_PCT )); then
  echo "CRITICAL: available memory is ${mem_available_pct}% <= ${MEM_AVAILABLE_CRIT_PCT}%" >&2
  status="CRITICAL"
elif (( mem_available_pct <= MEM_AVAILABLE_WARN_PCT )); then
  echo "WARNING: available memory is ${mem_available_pct}% <= ${MEM_AVAILABLE_WARN_PCT}%" >&2
  [[ "$status" == "OK" ]] && status="WARNING"
fi

echo "resource check: status=$status disk_used=${disk_used_pct}% mem_available=${mem_available_pct}%"

if [[ "$status" == "CRITICAL" ]]; then
  exit 2
fi
if [[ "$status" == "WARNING" ]]; then
  exit 1
fi
