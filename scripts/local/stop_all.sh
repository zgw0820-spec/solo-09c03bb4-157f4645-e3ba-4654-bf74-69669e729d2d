#!/usr/bin/env bash
# Stop the local (no-docker) stack: API + PostgreSQL.
set -euo pipefail
cd "$(dirname "$0")/../.."

VENV=${VENV:-.venv}
RUN=.run

if [ -f "$RUN/app.pid" ] && kill -0 "$(cat "$RUN/app.pid")" 2>/dev/null; then
  kill "$(cat "$RUN/app.pid")" || true
  echo "app stopped"
fi

PGBIN=$("$VENV/bin/python" - <<'EOF'
import os, pgserver
print(os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin"))
EOF
)
if [ -d "$RUN/pgdata" ]; then
  "$PGBIN/pg_ctl" -D "$RUN/pgdata" -m fast stop && echo "postgres stopped"
fi
