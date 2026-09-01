#!/usr/bin/env bash
# Stop the natively-running AiNxt API and UI started by ./install.sh --local.
# The datastores keep running; stop those with: docker compose stop postgres redis ollama
set -uo pipefail
stopped=0
for f in .ainxt-gateway.pid .ainxt-ui.pid; do
  [[ -f "$f" ]] || continue
  pid="$(cat "$f")"
  if kill -0 "$pid" 2>/dev/null; then
    # negative pid kills the process group, catching gunicorn workers and vite children
    kill "$pid" 2>/dev/null || true
    pkill -P "$pid" 2>/dev/null || true
    echo "  stopped $f (pid $pid)"
    stopped=1
  fi
  rm -f "$f"
done
(( stopped )) || echo "  nothing running (no pid files)"
echo "  datastores still up — stop them with: docker compose stop postgres redis ollama"
