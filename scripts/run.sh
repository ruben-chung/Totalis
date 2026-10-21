#!/usr/bin/env bash
# Start one rfqbot process exactly once, detached, with caffeinate and a log file.
# Usage: scripts/run.sh <module> [args...]     e.g. scripts/run.sh rfqbot.books
#        scripts/run.sh rfqbot listen            (top-level CLI subcommands work too)
# Refuses to start if a process for that module is already running (the cause of a
# duplicate Kalshi feed on 2026-10-20). Logs go to data/logs/<module>.log.
set -euo pipefail
cd "$(dirname "$0")/.."
mod="${1:?usage: scripts/run.sh <module> [args...]}"; shift || true
pattern="python -m ${mod}( |$)"
if pgrep -f "$pattern" >/dev/null 2>&1; then
  echo "refusing: '$mod' already running:" >&2
  pgrep -fl "$pattern" | grep -v pgrep >&2
  exit 2
fi
mkdir -p data/logs
log="data/logs/${mod//[.\/]/_}.log"
nohup caffeinate -i .venv/bin/python -m "$mod" "$@" >> "$log" 2>&1 &
sleep 1
if pgrep -f "$pattern" >/dev/null 2>&1; then
  echo "started '$mod $*' -> $log"; pgrep -fl "$pattern" | grep -v pgrep | grep -v caffeinate
else
  echo "failed to start '$mod'; tail of $log:" >&2; tail -20 "$log" >&2; exit 1
fi
