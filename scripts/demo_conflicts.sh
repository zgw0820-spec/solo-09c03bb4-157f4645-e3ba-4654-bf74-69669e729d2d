#!/usr/bin/env bash
# Walkthrough of the "catalog reality" cases behind /foreign-key/status,
# /enable and /validate.  Destructive: it drops/recreates foreign keys on
# the sample database.  Run it on a scratch stack, then reset with
# scripts/local/reset_db.sh (or `docker compose down -v`).
set -euo pipefail
cd "$(dirname "$0")/.."

BASE=${BASE_URL:-http://localhost:8000}
PG_DSN=${PG_DSN:-postgresql://postgres:postgres@localhost:5432/ops}
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

step() { echo; echo "=================================================================="; echo "STEP $1: $2"; echo "=================================================================="; }
show() { "$PY" -m json.tool 2>/dev/null || cat; }
psqlc() { PG_DSN="$PG_DSN" "$PY" - "$@" <<'EOF'
import os, sys, psycopg
with psycopg.connect(os.environ["PG_DSN"], autocommit=True) as c:
    for stmt in sys.argv[1:]:
        c.execute(stmt)
EOF
}

step 1 "manual FK under a different name: status must find it by definition"
psqlc "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_inspections_device" \
      "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_manual_2024" \
      "ALTER TABLE inspections ADD CONSTRAINT fk_manual_2024 FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID"
curl -sS "$BASE/foreign-key/status" | show

step 2 "POST /enable: reuses it, adds no duplicate"
curl -sS -X POST "$BASE/foreign-key/enable" | show

step 3 "POST /validate: selects by OID, so a rename is harmless"
psqlc "ALTER TABLE inspections RENAME CONSTRAINT fk_manual_2024 TO fk_ops_after_rename" \
      "DELETE FROM inspections i WHERE i.device_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM devices d WHERE d.id = i.device_id)"
curl -sS -X POST "$BASE/foreign-key/validate" | show

step 4 "lookalike under the default name: status reports exists:false"
psqlc "ALTER TABLE inspections DROP CONSTRAINT fk_ops_after_rename" \
      "CREATE TABLE IF NOT EXISTS device_archive (id BIGINT PRIMARY KEY, name TEXT)" \
      "ALTER TABLE inspections ADD CONSTRAINT fk_inspections_device FOREIGN KEY (device_id) REFERENCES device_archive (id) NOT VALID"
curl -sS "$BASE/foreign-key/status" | show

step 5 "POST /enable: name taken by an unrelated FK -> 409, constraint kept"
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/foreign-key/enable"

step 6 "the original constraint is untouched"
psqlc "SELECT 1" >/dev/null
PG_DSN="$PG_DSN" "$PY" - <<'EOF'
import os, psycopg
with psycopg.connect(os.environ["PG_DSN"], autocommit=True) as c:
    print(c.execute(
        "SELECT conname, convalidated, pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE conrelid = 'inspections'::regclass AND contype = 'f'"
    ).fetchall())
EOF

echo
echo "Reset the sample database before running the normal demo again."
