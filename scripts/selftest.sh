#!/usr/bin/env bash
# Full self-test on Docker Compose: fresh stack, sample data, pytest suite.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== rebuilding stack with a fresh database volume =="
docker compose down -v --remove-orphans
docker compose up -d --build

echo "== waiting for the API =="
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS http://localhost:8000/health && echo

echo "== running pytest suite =="
BASE_URL=http://localhost:8000 \
PG_DSN=postgresql://postgres:postgres@localhost:5432/ops \
APP_RESTART_CMD="docker compose restart" \
  python3 -m pytest tests/ -v "$@"

echo
echo "== final FK status =="
curl -fsS http://localhost:8000/foreign-key/status && echo
echo
echo "Stack left running. Swagger UI: http://localhost:8000/docs"
