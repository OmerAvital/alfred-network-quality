#!/bin/sh
# Pulse handler, runs up to 10x/s. While a test runs, the runner has already drawn the next
# eased frame, so just print it (a few ms). Otherwise fall back to the Python renderer.
D="${alfred_workflow_data:-$HOME/Library/Application Support/Alfred/Workflow Data/dev.omeravital.networkQuality}"
: > "$D/ui.heartbeat"
pid=$(sed -n 's/.*"pid": \([0-9]*\).*/\1/p' "$D/run.lock" 2>/dev/null)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && [ -f "$D/frame.json" ]; then
  cat "$D/frame.json"
else
  exec /usr/bin/python3 "$(dirname "$0")/nq.py" pulse "$1"
fi
