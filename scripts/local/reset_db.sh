#!/usr/bin/env bash
# Reset the local sample database to its pristine (pre-migration) state.
set -euo pipefail
cd "$(dirname "$0")/../.."

VENV=${VENV:-.venv}
RUN=.run
PGPORT=55432
PGHOST=127.0.0.1

PGBIN=$("$VENV/bin/python" - <<'EOF'
import os, pgserver
print(os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin"))
EOF
)

"$PGBIN/dropdb" --if-exists -p "$PGPORT" -h "$PGHOST" -U postgres ops
"$PGBIN/createdb" -p "$PGPORT" -h "$PGHOST" -U postgres ops
"$PGBIN/psql" -p "$PGPORT" -h "$PGHOST" -U postgres -d ops -v ON_ERROR_STOP=1 -q -f db/init.sql
echo "sample database 'ops' reset to pre-migration state"
