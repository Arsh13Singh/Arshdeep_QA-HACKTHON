#!/usr/bin/env bash
# DEV ONLY: (re)start the local DemoBank target used while developing the agent.
#   ./devtools/dev_target.sh            -> stand-in backend :8000 (fresh seed) + bundled real frontend :5173
# Prefer the real stack from test-website/LOCAL_SETUP.md whenever pip/npm/docker are available.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
KIT="$HERE/../../test-website"
DIST="${DIST:-/tmp/demobank-dist}"
# The [s] trick stops pkill from matching this script's own command line.
pkill -f "[s]tandin_backend.py" || true
pkill -f "[s]pa_server.py" || true
sleep 1
[ -f "$DIST/index.html" ] || node "$HERE/build_frontend.mjs" "$KIT/frontend" "$DIST"
nohup python3 "$HERE/standin_backend.py" --seed "$KIT/database/seed.sql" --port 8000 >/dev/null 2>&1 &
nohup python3 "$HERE/spa_server.py" "$DIST" 5173 >/dev/null 2>&1 &
sleep 2
curl -sf localhost:8000/health >/dev/null && curl -sf localhost:5173/ >/dev/null && echo "DemoBank dev target up: http://localhost:5173"
