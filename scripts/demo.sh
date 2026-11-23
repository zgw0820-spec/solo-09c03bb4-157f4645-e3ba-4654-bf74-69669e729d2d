#!/usr/bin/env bash
# Guided walkthrough of the online FK migration, step by step.
# Every step maps 1:1 to an operation in the Swagger UI (/docs).
# Requires a fresh sample database (see scripts/selftest.sh or
# scripts/local/run_local.sh, which reset it).
set -euo pipefail
cd "$(dirname "$0")/.."

BASE=${BASE_URL:-http://localhost:8000}
PG_DSN=${PG_DSN:-postgresql://postgres:postgres@localhost:5432/ops}
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

step() { echo; echo "=================================================================="; echo "STEP $1: $2"; echo "=================================================================="; }
show() { "$PY" -m json.tool 2>/dev/null || cat; }

step 1 "GET /foreign-key/status  (no constraint yet)"
curl -sS "$BASE/foreign-key/status" | show

step 2 "GET /inspections/orphans  (legacy rows pointing at deleted devices)"
curl -sS "$BASE/inspections/orphans" | show

step 3 "POST /inspections  (before protection an orphan can still be created)"
curl -sS -X POST "$BASE/inspections" -H 'Content-Type: application/json' \
  -d '{"device_id": 9999, "note": "collector row for a removed device"}' | show

step 4 "POST /foreign-key/enable  (ADD CONSTRAINT ... NOT VALID: protection ON, no table scan)"
curl -sS -X POST "$BASE/foreign-key/enable" | show

step 5 "POST /inspections  (same write is now refused by the database)"
curl -sS -o /dev/stderr -w "HTTP %{http_code}\n" -X POST "$BASE/inspections" \
  -H 'Content-Type: application/json' \
  -d '{"device_id": 9999, "note": "blocked now"}' 2>&1 | tail -2

step 6 "direct SQL writes (the collector bypasses the API; DB still polices)"
PG_DSN="$PG_DSN" "$PY" - <<'EOF'
import os, psycopg
dsn = os.environ["PG_DSN"]
with psycopg.connect(dsn, autocommit=True) as c:
    try:
        c.execute("INSERT INTO inspections (device_id, note) VALUES (9999, 'direct bad')")
        print("bad direct insert: ACCEPTED (unexpected!)")
    except psycopg.errors.ForeignKeyViolation as e:
        print(f"bad direct insert: rejected by PostgreSQL -> {e.diag.message_primary}")
    c.execute("INSERT INTO inspections (device_id, note) VALUES (1, 'direct ok')")
    print("good direct insert: committed")
EOF

step 7 "DELETE /devices/1  (referenced device: database refuses)"
curl -sS -w "\nHTTP %{http_code}\n" -X DELETE "$BASE/devices/1"

step 8 "POST /foreign-key/validate  (fails while orphans exist; protection stays ON)"
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/foreign-key/validate"

step 9 "POST /devices  (operator creates a device to attach orphans to)"
POOL_ID=$(curl -sS -X POST "$BASE/devices" -H 'Content-Type: application/json' \
  -d '{"name": "decommissioned-pool", "location": "archive"}' | "$PY" -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "created device id=$POOL_ID"

step 10 "POST /inspections/{id}/reassign  (wrong expected_device_id -> 409 conflict)"
FIRST=$(curl -sS "$BASE/inspections/orphans" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['rows'][0]['id'])")
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/inspections/$FIRST/reassign" \
  -H 'Content-Type: application/json' \
  -d "{\"expected_device_id\": 12345, \"new_device_id\": $POOL_ID}"

step 11 "POST /inspections/{id}/reassign  (fix every orphan with the seen device_id)"
curl -sS "$BASE/inspections/orphans" | "$PY" -c "
import sys, json
for r in json.load(sys.stdin)['rows']:
    print(r['id'], r['device_id'])
" | while read -r RID DEV; do
  curl -sS -X POST "$BASE/inspections/$RID/reassign" -H 'Content-Type: application/json' \
    -d "{\"expected_device_id\": $DEV, \"new_device_id\": $POOL_ID}"
  echo
done

step 12 "GET /inspections/orphans  (clean now)"
curl -sS "$BASE/inspections/orphans" | show

step 13 "POST /foreign-key/validate  (full-table scan; writes keep flowing)"
curl -sS -X POST "$BASE/foreign-key/validate" | show

step 14 "GET /foreign-key/status  (validated; survives restarts - catalog state)"
curl -sS "$BASE/foreign-key/status" | show

echo
echo "Migration complete. Try it yourself in Swagger UI: $BASE/docs"
