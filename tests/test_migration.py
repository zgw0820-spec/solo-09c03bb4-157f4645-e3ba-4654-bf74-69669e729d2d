"""End-to-end tests for the online FK migration.

Runs against a live stack (docker compose, or the local runner in
scripts/local/). State is mutated in order, so tests are sequential.

Environment:
  BASE_URL         API base URL          (default http://localhost:8000)
  PG_DSN           direct PostgreSQL DSN (default ...@localhost:5432/ops)
  APP_RESTART_CMD  command that restarts the app (default "docker compose restart")
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

import httpx
import psycopg
import pytest

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
PG_DSN = os.environ.get(
    "PG_DSN", "postgresql://postgres:postgres@localhost:5432/ops"
)
APP_RESTART_CMD = os.environ.get("APP_RESTART_CMD", "docker compose restart")
REPO_ROOT = Path(__file__).resolve().parent.parent

FK = "fk_inspections_device"
CLIENT = httpx.Client(base_url=BASE, timeout=30)


def fk_status() -> dict:
    r = CLIENT.get("/foreign-key/status")
    assert r.status_code == 200
    return r.json()


def orphans() -> dict:
    r = CLIENT.get("/inspections/orphans")
    assert r.status_code == 200
    return r.json()


# --------------------------------------------------------------------------
# 1. before the migration
# --------------------------------------------------------------------------

def test_01_swagger_ui_available():
    r = CLIENT.get("/docs")
    assert r.status_code == 200


def test_02_initial_state_no_constraint():
    st = fk_status()
    assert st["exists"] is False
    assert st["enforced"] is False
    assert st["validated"] is False


def test_03_orphans_visible():
    data = orphans()
    assert data["count"] >= 4
    missing_devices = {row["device_id"] for row in data["rows"]}
    assert {6, 7, 8} <= missing_devices


def test_04_legacy_orphan_insert_allowed_before_protection():
    # the collector can still create orphans before protection is enabled
    r = CLIENT.post("/inspections", json={"device_id": 9999, "note": "legacy orphan"})
    assert r.status_code == 201


# --------------------------------------------------------------------------
# 2. enable protection (ADD CONSTRAINT ... NOT VALID)
# --------------------------------------------------------------------------

def test_05_enable_protection():
    r = CLIENT.post("/foreign-key/enable")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True
    assert body["exists"] is True
    assert body["enforced"] is True
    assert body["validated"] is False  # NOT VALID yet


def test_06_enable_is_idempotent():
    r = CLIENT.post("/foreign-key/enable")
    assert r.status_code == 200
    assert r.json()["changed"] is False


def test_07_api_insert_bad_device_rejected():
    r = CLIENT.post("/inspections", json={"device_id": 8888, "note": "bad"})
    assert r.status_code == 409


def test_08_direct_db_writes_policed_by_database():
    # the collector bypasses the API and writes straight to PostgreSQL
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO inspections (device_id, note) VALUES (8888, 'collector bad')"
            )
        # legitimate direct writes still commit
        conn.execute(
            "INSERT INTO inspections (device_id, note) VALUES (1, 'collector ok')"
        )


def test_09_delete_referenced_device_refused():
    r = CLIENT.delete("/devices/1")
    assert r.status_code == 409


def test_10_delete_unreferenced_device_ok():
    r = CLIENT.post("/devices", json={"name": "scratch", "location": "tmp"})
    dev_id = r.json()["id"]
    r = CLIENT.delete(f"/devices/{dev_id}")
    assert r.status_code == 200


# --------------------------------------------------------------------------
# 3. validation fails while orphans exist; protection stays on
# --------------------------------------------------------------------------

def test_11_validate_fails_while_orphans_remain():
    r = CLIENT.post("/foreign-key/validate")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["orphan_count"] >= 5  # 4 seeded + 1 from test_04
    assert detail["orphan_sample"]
    # write protection is still on afterwards
    assert CLIENT.post("/inspections", json={"device_id": 8888}).status_code == 409
    st = fk_status()
    assert st["exists"] is True and st["validated"] is False


# --------------------------------------------------------------------------
# 4. operator cleans up orphans (optimistic reassign)
# --------------------------------------------------------------------------

def test_12_reassign_conflict_when_device_id_changed():
    row = orphans()["rows"][0]
    r = CLIENT.post(
        f"/inspections/{row['id']}/reassign",
        json={"expected_device_id": row["device_id"] + 1, "new_device_id": 1},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["current_device_id"] == row["device_id"]


def test_13_reassign_to_missing_device_rejected():
    row = orphans()["rows"][0]
    r = CLIENT.post(
        f"/inspections/{row['id']}/reassign",
        json={"expected_device_id": row["device_id"], "new_device_id": 424242},
    )
    assert r.status_code == 409


def test_14_reassign_unknown_record_404():
    r = CLIENT.post(
        "/inspections/99999999/reassign",
        json={"expected_device_id": 1, "new_device_id": 1},
    )
    assert r.status_code == 404


def test_15_fix_all_orphans():
    r = CLIENT.post(
        "/devices", json={"name": "decommissioned-pool", "location": "archive"}
    )
    pool_id = r.json()["id"]
    for row in orphans()["rows"]:
        resp = CLIENT.post(
            f"/inspections/{row['id']}/reassign",
            json={"expected_device_id": row["device_id"], "new_device_id": pool_id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["device_id"] == pool_id
    assert orphans()["count"] == 0


# --------------------------------------------------------------------------
# 5. validation succeeds while legitimate writes keep flowing
# --------------------------------------------------------------------------

def test_16_validate_succeeds_with_concurrent_writes():
    # bulk-load rows so the validation scan takes a measurable amount of time
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO inspections (device_id, note) "
            "SELECT 1 + (g % 5), 'bulk' FROM generate_series(1, 1500000) AS g"
        )

    result: dict = {}

    def run_validate():
        r = CLIENT.post("/foreign-key/validate", timeout=300)
        result["status"] = r.status_code
        result["body"] = r.json()

    t = threading.Thread(target=run_validate)
    t.start()

    # wait until PostgreSQL is actually running VALIDATE CONSTRAINT
    deadline = time.time() + 60
    validating = False
    while time.time() < deadline and t.is_alive():
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            n = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query LIKE 'ALTER TABLE%VALIDATE CONSTRAINT%' AND state = 'active'"
            ).fetchone()[0]
        if n:
            validating = True
            break
        time.sleep(0.02)
    assert validating, "never observed VALIDATE CONSTRAINT in pg_stat_activity"

    # while validation scans the table: legitimate writes commit,
    # illegitimate ones are still rejected by the NOT VALID constraint
    committed = 0
    rejected = 0
    with psycopg.connect(PG_DSN, autocommit=True) as good, psycopg.connect(
        PG_DSN, autocommit=True
    ) as bad:
        while t.is_alive():
            good.execute(
                "INSERT INTO inspections (device_id, note) VALUES (2, 'during-validate')"
            )
            committed += 1
            try:
                bad.execute(
                    "INSERT INTO inspections (device_id, note) VALUES (7777, 'bad during-validate')"
                )
            except psycopg.errors.ForeignKeyViolation:
                rejected += 1
    t.join()

    assert committed > 0, "no legitimate write committed during validation"
    assert rejected > 0, "no illegitimate write rejected during validation"
    print(f"\ninterleaved during VALIDATE: {committed} committed, {rejected} rejected")
    assert result["status"] == 200, result
    assert result["body"]["validated"] is True

    st = fk_status()
    assert st["exists"] is True and st["validated"] is True
    assert "FOREIGN KEY" in st["definition"]


def test_17_validate_is_idempotent():
    r = CLIENT.post("/foreign-key/validate")
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["validated"] is True


# --------------------------------------------------------------------------
# 6. restart: status still reflects the real catalog state
# --------------------------------------------------------------------------

def test_18_status_survives_restart():
    try:
        subprocess.run(
            shlex.split(APP_RESTART_CMD),
            check=True,
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=180,
        )
    except FileNotFoundError as exc:
        pytest.skip(f"restart command unavailable: {exc}")

    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            if CLIENT.get("/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        pytest.fail("app did not come back after restart")

    st = fk_status()
    assert st["exists"] is True
    assert st["enforced"] is True
    assert st["validated"] is True
    # and protection is still real after the restart
    assert CLIENT.post("/inspections", json={"device_id": 8888}).status_code == 409
