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


def db_exec(*statements: str, params: tuple | None = None):
    """Run DDL / setup statements directly as the collector would."""
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        for stmt in statements:
            conn.execute(stmt, params)


def constraint_row(name: str):
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        return conn.execute(
            "SELECT convalidated, pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE conrelid = 'inspections'::regclass AND conname = %s",
            (name,),
        ).fetchone()


def restore_default_fk() -> None:
    """Reset to a single, validated default-named FK for the next test."""
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_inspections_device",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_manual_site_fk",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_manual_dup",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_legacy_manual",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_rename_race",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_renamed",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_race_old",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_race_renamed",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_race_new",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_archive_named",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_deferred",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_cascade",
        "DELETE FROM inspections i WHERE i.device_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM devices d WHERE d.id = i.device_id)",
        "ALTER TABLE inspections ADD CONSTRAINT fk_inspections_device "
        "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID",
        "ALTER TABLE inspections VALIDATE CONSTRAINT fk_inspections_device",
    )


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


# --------------------------------------------------------------------------
# 7. identity is by definition, not by name
# --------------------------------------------------------------------------

def test_19_equivalent_fk_under_other_name_is_reused():
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "ALTER TABLE inspections ADD CONSTRAINT fk_legacy_manual "
        "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID",
        "DELETE FROM inspections WHERE device_id = 8888",  # no orphans for now
    )
    st = fk_status()
    assert st["exists"] is True
    assert st["constraint"] == "fk_legacy_manual"
    assert st["validated"] is False

    r = CLIENT.post("/foreign-key/enable")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is False           # reused, not duplicated
    assert body["constraint"] == "fk_legacy_manual"
    assert constraint_row("fk_inspections_device") is None

    r = CLIENT.post("/foreign-key/validate")
    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert r.json()["validated"] is True
    assert constraint_row("fk_legacy_manual")[0] is True
    restore_default_fk()


def test_20_prefers_validated_among_duplicates():
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "ALTER TABLE inspections ADD CONSTRAINT fk_manual_dup "
        "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID",
        "ALTER TABLE inspections ADD CONSTRAINT fk_inspections_device "
        "FOREIGN KEY (device_id) REFERENCES devices (id)",
    )
    st = fk_status()
    assert st["constraint"] == "fk_inspections_device"
    assert st["validated"] is True
    assert set(st["equivalent_constraints"]) == {
        "fk_manual_dup", "fk_inspections_device",
    }
    r = CLIENT.post("/foreign-key/enable")
    assert r.json()["changed"] is False
    assert r.json()["validated"] is True
    r = CLIENT.post("/foreign-key/validate")
    assert r.status_code == 200 and r.json()["changed"] is False
    restore_default_fk()


def test_21_lookalike_constraints_are_ignored():
    """Same name, wrong target; same target, DEFERRABLE; same target,
    ON DELETE CASCADE -- none of them counts as the FK we manage."""
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_deferred",
        "ALTER TABLE inspections DROP CONSTRAINT IF EXISTS fk_cascade",
        "CREATE TABLE IF NOT EXISTS device_archive "
        "(id BIGINT PRIMARY KEY, name TEXT)",
        "ALTER TABLE inspections ADD CONSTRAINT fk_archive_named "
        "FOREIGN KEY (device_id) REFERENCES device_archive (id) NOT VALID",
    )
    st = fk_status()
    assert st["exists"] is False, st
    r = CLIENT.post("/foreign-key/enable")
    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert r.json()["constraint"] == "fk_inspections_device"

    # now the valid FK is in place; prove the lookalikes still count for
    # nothing in status / enable / validate
    db_exec(
        "ALTER TABLE inspections ADD CONSTRAINT fk_deferred "
        "FOREIGN KEY (device_id) REFERENCES devices (id) "
        "DEFERRABLE INITIALLY DEFERRED NOT VALID",
        "ALTER TABLE inspections ADD CONSTRAINT fk_cascade "
        "FOREIGN KEY (device_id) REFERENCES devices (id) "
        "ON DELETE CASCADE NOT VALID",
        "DELETE FROM inspections WHERE device_id = 8888",
    )
    st = fk_status()
    assert st["equivalent_constraints"] == ["fk_inspections_device"]
    assert constraint_row("fk_archive_named")[0] is False   # untouched
    assert constraint_row("fk_deferred")[0] is False
    assert constraint_row("fk_cascade")[0] is False
    # validate only touches the selected constraint
    assert CLIENT.post("/foreign-key/validate").status_code == 200
    assert constraint_row("fk_inspections_device")[0] is True
    assert constraint_row("fk_deferred")[0] is False
    assert constraint_row("fk_cascade")[0] is False
    # ... and status does not mistake the cascade FK for protection either
    db_exec("ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device")
    st = fk_status()
    assert st["exists"] is False
    assert constraint_row("fk_deferred")[0] is False
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_deferred",
        "ALTER TABLE inspections DROP CONSTRAINT fk_cascade",
    )
    restore_default_fk()


def test_22_default_name_taken_by_unrelated_constraint_conflicts():
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "ALTER TABLE inspections ADD CONSTRAINT fk_inspections_device "
        "FOREIGN KEY (device_id) REFERENCES device_archive (id) NOT VALID",
    )
    r = CLIENT.post("/foreign-key/enable")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "device_archive" in detail["existing"]["definition"]
    # the unrelated constraint is still there, unchanged, and no equivalent
    # FK was silently added
    assert constraint_row("fk_inspections_device") == (
        False,
        "FOREIGN KEY (device_id) REFERENCES device_archive(id) NOT VALID",
    )
    assert fk_status()["exists"] is False
    restore_default_fk()
    assert fk_status()["validated"] is True


def test_23_first_enable_does_not_scan_legacy_rows():
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "INSERT INTO inspections (device_id, note) VALUES (424242, 'legacy-noisy')",
    )
    r = CLIENT.post("/foreign-key/enable")
    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert r.json()["validated"] is False       # no scan
    assert orphans()["count"] >= 1              # old orphan survived
    # protection is on for new writes all the same
    assert CLIENT.post("/inspections", json={"device_id": 424242}).status_code == 409
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM inspections WHERE note = 'legacy-noisy'")
    restore_default_fk()


# --------------------------------------------------------------------------
# 8. stale orphans and the 409 path
# --------------------------------------------------------------------------

def test_24_validation_failure_keeps_write_protection():
    """A stale orphan makes the scan fail; the NOT VALID constraint keeps
    rejecting bad writes before, during and after the failed scan."""
    # seed the orphan while protection is OFF (as a legacy row would be)
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "INSERT INTO inspections (device_id, note) VALUES (424243, 'legacy-stale')",
        "ALTER TABLE inspections ADD CONSTRAINT fk_inspections_device "
        "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID",
    )
    r = CLIENT.post("/foreign-key/validate")
    assert r.status_code == 409
    assert r.json()["detail"]["orphan_count"] >= 1
    assert constraint_row("fk_inspections_device")[0] is False
    # protection did not lapse
    assert CLIENT.post("/inspections", json={"device_id": 424243}).status_code == 409
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO inspections (device_id, note) VALUES (424243, 'bad')"
            )
        # legitimate writes are not blocked by the failed scan
        conn.execute(
            "INSERT INTO inspections (device_id, note) VALUES (1, 'still flowing')"
        )
        conn.execute("DELETE FROM inspections WHERE note = 'legacy-stale'")
    assert CLIENT.post("/foreign-key/validate").status_code == 200
    restore_default_fk()


def test_25_enable_after_name_conflict_keeps_existing_constraint():
    """The 409 path must be repeatable and must never drop/alter the
    unrelated constraint occupying the default name."""
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "ALTER TABLE inspections ADD CONSTRAINT fk_inspections_device "
        "FOREIGN KEY (device_id) REFERENCES device_archive (id) NOT VALID",
    )
    before = constraint_row("fk_inspections_device")
    for _ in range(2):
        r = CLIENT.post("/foreign-key/enable")
        assert r.status_code == 409
        assert constraint_row("fk_inspections_device") == before
    assert fk_status()["exists"] is False
    restore_default_fk()


# --------------------------------------------------------------------------
# 9. validation is tied to the selected constraint
# --------------------------------------------------------------------------

def test_26_validate_selected_renamed_constraint():
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "ALTER TABLE inspections ADD CONSTRAINT fk_rename_race "
        "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID",
        "ALTER TABLE inspections RENAME CONSTRAINT fk_rename_race TO fk_renamed",
    )
    st = fk_status()
    assert st["constraint"] == "fk_renamed"
    r = CLIENT.post("/foreign-key/validate")
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True
    assert r.json()["constraint"] == "fk_renamed"
    assert constraint_row("fk_renamed")[0] is True
    restore_default_fk()


def _validate_while_catalog_changes(change_stmts: list[str]) -> dict:
    """Queue /foreign-key/validate behind an ACCESS EXCLUSIVE lock, change
    the FK from another session (rename / drop+replace), then release.
    Returns the API response recorded by the background thread."""
    result: dict = {}

    def run_validate():
        r = CLIENT.post("/foreign-key/validate", timeout=120)
        result["status"] = r.status_code
        result["body"] = r.json()

    with psycopg.connect(PG_DSN, autocommit=True) as locker:
        locker.execute("BEGIN")
        locker.execute("LOCK TABLE inspections IN ACCESS EXCLUSIVE MODE")
        t = threading.Thread(target=run_validate)
        t.start()
        time.sleep(0.5)  # let the request select the FK and queue on LOCK
        for stmt in change_stmts:
            locker.execute(stmt)
        locker.execute("COMMIT")
        t.join()
    return result


def test_27_validate_never_validates_a_replaced_constraint():
    """The selected FK is dropped and replaced (new OID) while VALIDATE waits
    for its lock.  We re-read by OID after taking the lock, so the stale
    selection is abandoned and the catalog is re-scanned; the old selection
    is never validated blindly."""
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "ALTER TABLE inspections ADD CONSTRAINT fk_race_old "
        "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID",
    )
    old_oid = None
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        old_oid = conn.execute(
            "SELECT oid FROM pg_constraint WHERE conname = 'fk_race_old'"
        ).fetchone()[0]

    result = _validate_while_catalog_changes([
        "ALTER TABLE inspections DROP CONSTRAINT fk_race_old",
        "ALTER TABLE inspections ADD CONSTRAINT fk_race_new "
        "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID",
    ])

    assert result["status"] in (200, 409), result
    if result["status"] == 200:
        assert result["body"]["constraint"] == "fk_race_new"
        assert result["body"]["validated"] is True
        assert result["body"]["oid"] != old_oid
        assert constraint_row("fk_race_new")[0] is True
    else:
        # conflict path: the old constraint is gone and the new one exists,
        # still NOT VALID; nothing was silently validated
        assert constraint_row("fk_race_old") is None
        assert constraint_row("fk_race_new")[0] is False
        assert fk_status()["constraint"] == "fk_race_new"
    restore_default_fk()


def test_28_validate_follows_selected_constraint_renamed_under_lock():
    """Renaming keeps the OID: after the lock is acquired the constraint is
    re-read by identity and validated under its new name."""
    db_exec(
        "ALTER TABLE inspections DROP CONSTRAINT fk_inspections_device",
        "ALTER TABLE inspections ADD CONSTRAINT fk_race_old "
        "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID",
    )
    result = _validate_while_catalog_changes([
        "ALTER TABLE inspections RENAME CONSTRAINT fk_race_old TO fk_race_renamed",
    ])
    assert result["status"] == 200, result
    assert result["body"]["constraint"] == "fk_race_renamed"
    assert result["body"]["validated"] is True
    assert constraint_row("fk_race_renamed")[0] is True
    restore_default_fk()

