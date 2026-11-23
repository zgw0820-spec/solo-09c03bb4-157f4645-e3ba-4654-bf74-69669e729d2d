#!/usr/bin/env bash
# Local (no-docker) twin of scripts/selftest.sh, for environments without a
# Docker daemon. Uses the PostgreSQL binaries bundled with the `pgserver`
# pip package and runs uvicorn directly. The app code, SQL, and pytest
# suite are exactly the same as in the Docker deployment.
set -euo pipefail
cd "$(dirname "$0")/../.."

VENV=${VENV:-.venv}
PY="$VENV/bin/python"
RUN=.run
export PGPORT=55432
export PGHOST=127.0.0.1
export PGUSER=postgres
export PGDATABASE=ops
DSN="postgresql://postgres@127.0.0.1:55432/ops"

mkdir -p "$RUN"

# --- locate the pg binaries bundled with pgserver -------------------------
PGBIN=$("$PY" - <<'EOF'
import os, pgserver
print(os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin"))
EOF
)
[ -x "$PGBIN/initdb" ] || { echo "pgserver binaries not found in $PGBIN"; exit 1; }
echo "== using PostgreSQL binaries from $PGBIN =="
"$PGBIN/postgres" --version

# --- (re)start postgres ---------------------------------------------------
if [ ! -s "$RUN/pgdata/PG_VERSION" ]; then
  echo "== initdb =="
  "$PGBIN/initdb" -D "$RUN/pgdata" -U postgres -A trust >/dev/null
fi
if ! "$PGBIN/pg_ctl" -D "$RUN/pgdata" status >/dev/null 2>&1; then
  echo "== starting postgres on 127.0.0.1:$PGPORT =="
  "$PGBIN/pg_ctl" -D "$RUN/pgdata" -l "$RUN/pg.log" -o "-p $PGPORT -c listen_addresses=127.0.0.1" -w start >/dev/null
fi

# --- fresh sample database -------------------------------------------------
echo "== resetting sample database 'ops' =="
bash scripts/local/reset_db.sh

# --- (re)start the API -----------------------------------------------------
echo "== starting uvicorn on :8000 =="
if [ -f "$RUN/app.pid" ] && kill -0 "$(cat "$RUN/app.pid")" 2>/dev/null; then
  kill "$(cat "$RUN/app.pid")" || true
  sleep 1
fi
DATABASE_URL="$DSN" nohup "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  >"$RUN/app.log" 2>&1 &
echo $! > "$RUN/app.pid"
echo "$DSN" > "$RUN/dsn"

for i in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS http://localhost:8000/health && echo

# --- run the suite -----------------------------------------------------------
echo "== running pytest suite =="
BASE_URL=http://localhost:8000 \
PG_DSN="$DSN" \
APP_RESTART_CMD="bash scripts/local/restart_app.sh" \
  "$PY" -m pytest tests/ -v "$@"

echo
echo "== final FK status =="
curl -fsS http://localhost:8000/foreign-key/status && echo
echo
echo "Stack left running. Swagger UI: http://localhost:8000/docs"
echo "Stop with: bash scripts/local/stop_all.sh"
