#!/usr/bin/env bash
# Restart only the API process (local no-docker runner). Used by the pytest
# restart test to prove FK status comes from the PG catalog, not memory.
set -euo pipefail
cd "$(dirname "$0")/../.."

VENV=${VENV:-.venv}
RUN=.run
DSN=$(cat "$RUN/dsn")

if [ -f "$RUN/app.pid" ] && kill -0 "$(cat "$RUN/app.pid")" 2>/dev/null; then
  kill "$(cat "$RUN/app.pid")"
  for i in $(seq 1 20); do
    kill -0 "$(cat "$RUN/app.pid")" 2>/dev/null || break
    sleep 0.2
  done
fi

DATABASE_URL="$DSN" nohup "$VENV/bin/python" -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 >>"$RUN/app.log" 2>&1 &
echo $! > "$RUN/app.pid"
