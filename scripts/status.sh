#!/usr/bin/env bash
# One-line health view of every rfqbot process and its latest log line.
cd "$(dirname "$0")/.."
printf "%-8s %-28s %s\n" PID MODULE "LAST LOG LINE"
pgrep -fl "python -m rfqbot" | grep -v -E "pgrep|caffeinate|/bin/bash" | while read -r pid rest; do
  mod=$(echo "$rest" | sed -E 's/.*python -m ([^ ]+).*/\1/')
  log="data/logs/${mod//[.\/]/_}.log"; [ -f "$log" ] || log="data/logs/${mod#rfqbot.}.log"
  last=$( [ -f "$log" ] && tail -1 "$log" | cut -c1-90 || echo "(no log at $log)")
  printf "%-8s %-28s %s\n" "$pid" "$mod" "$last"
done
