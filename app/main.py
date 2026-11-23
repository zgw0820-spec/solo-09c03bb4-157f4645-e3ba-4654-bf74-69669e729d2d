"""FastAPI service driving the online foreign-key migration.

Flow (all steps are idempotent and safe to re-run):

  GET  /foreign-key/status    actual FK state from the PG catalog
  POST /foreign-key/enable    add FK as NOT VALID (write protection ON)
  GET  /inspections/orphans   list legacy rows pointing at missing devices
  POST /inspections/{id}/reassign  optimistic re-attach to a real device
  POST /foreign-key/validate  full-table validation (writes keep flowing)
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from psycopg.errors import DuplicateObject, ForeignKeyViolation
from pydantic import BaseModel, Field

from .db import (
    ADD_FK_SQL,
    CONSTRAINT_BY_NAME_SQL,
    EQUIVALENT_FK_BY_OID_SQL,
    EQUIVALENT_FKS_SQL,
    FK_NAME,
    LOCK_FOR_VALIDATE_SQL,
    ORPHAN_WHERE,
    connect,
    validate_fk_sql,
)

app = FastAPI(
    title="Online FK migration API",
    version="1.1.0",
    description=(
        "Adds the `inspections.device_id -> devices.id` foreign key online, "
        "while the collector keeps writing directly to PostgreSQL. "
        "The managed constraint is identified by definition (single column, "
        "NOT DEFERRABLE, NO ACTION) rather than by name, so manually added "
        "constraints are recognised and reused. "
        "Enable protection first, clean up orphan rows, then validate."
    ),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fk_row_to_status(row) -> dict[str, Any]:
    oid, name, validated, definition = row
    return {
        "constraint": name,
        "oid": oid,
        "exists": True,
        "enforced": True,
        "validated": validated,
        "definition": definition,
    }


def find_equivalent_fks(conn) -> list[dict[str, Any]]:
    """All foreign keys equivalent to the one we manage, best first.

    Identification is by definition (single column inspections.device_id ->
    devices.id, NOT DEFERRABLE, NO ACTION on update and delete), never by
    name: a manually added constraint with a different name still counts.
    Validated constraints sort first.
    """
    rows = conn.execute(EQUIVALENT_FKS_SQL).fetchall()
    return [_fk_row_to_status(r) for r in rows]


def read_fk_status() -> dict[str, Any]:
    """Actual constraint state from the PostgreSQL catalog.

    The API keeps no in-memory state, so this is correct across restarts.
    A correctly defined constraint is reported whatever its name; unrelated
    foreign keys (e.g. one aimed at an archive device table) are ignored.
    """
    with connect() as conn:
        found = find_equivalent_fks(conn)
    if not found:
        return {
            "constraint": None,
            "oid": None,
            "exists": False,
            "enforced": False,
            "validated": False,
            "definition": None,
            "equivalent_constraints": [],
        }
    best = found[0]
    return {**best, "equivalent_constraints": [f["constraint"] for f in found]}


def orphan_count(conn) -> int:
    return conn.execute(f"SELECT count(*) {ORPHAN_WHERE}").fetchone()[0]


def orphan_rows(conn, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT i.id, i.device_id, i.note, i.created_at {ORPHAN_WHERE}"
        " ORDER BY i.id LIMIT %s",
        (limit,),
    ).fetchall()
    return [
        {"id": r[0], "device_id": r[1], "note": r[2], "created_at": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/health", include_in_schema=False)
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# foreign key lifecycle
# ---------------------------------------------------------------------------

@app.get("/foreign-key/status", tags=["foreign-key"])
def fk_status():
    """Actual enabled/validated state of the FK, read from the catalog."""
    return read_fk_status()


@app.post("/foreign-key/enable", tags=["foreign-key"])
def fk_enable():
    """Step 1: add the FK as NOT VALID (no table scan).

    Brief catalog lock only, no table scan. From this moment the database
    rejects new/updated inspections pointing at missing devices and refuses
    to delete devices that are still referenced -- even for clients that
    bypass this API and write to PostgreSQL directly.

    Idempotent by definition, not by name: if a manually created constraint
    already has the right shape, it is reused (and reported as it is). Only
    when no equivalent constraint exists do we try to add one under the
    default name; if that name is taken by something else, return 409 and
    leave the existing constraint untouched.
    """
    with connect(autocommit=True) as conn:
        existing = find_equivalent_fks(conn)
        if existing:
            best = existing[0]
            return {
                "changed": False,
                **best,
                "equivalent_constraints": [f["constraint"] for f in existing],
            }
        try:
            conn.execute(ADD_FK_SQL)
        except DuplicateObject:
            # Either a concurrent enabler won the race (then an equivalent FK
            # now exists), or the default name belongs to an unrelated
            # constraint -- which we must not touch.
            existing = find_equivalent_fks(conn)
            if existing:
                best = existing[0]
                return {
                    "changed": False,
                    **best,
                    "equivalent_constraints": [
                        f["constraint"] for f in existing
                    ],
                }
            clash = conn.execute(
                CONSTRAINT_BY_NAME_SQL, {"name": FK_NAME}
            ).fetchone()
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        f"constraint name {FK_NAME!r} is already used by a "
                        "different constraint; it was left unchanged"
                    ),
                    "constraint": FK_NAME,
                    "existing": (
                        {
                            "name": clash[0],
                            "type": clash[1],
                            "validated": clash[2],
                            "definition": clash[3],
                        }
                        if clash is not None
                        else None
                    ),
                },
            )
        return {"changed": True, **read_fk_status()}


@app.post("/foreign-key/validate", tags=["foreign-key"])
def fk_validate():
    """Step 3: validate the equivalent FK against the whole table.

    The exact constraint is selected from the catalog (by definition, not by
    name) and then locked together with the VALIDATE: both run in one
    transaction after ``LOCK TABLE ... SHARE UPDATE EXCLUSIVE``, which allows
    ordinary reads and writes but blocks RENAME / DROP / REPLACE.  If the
    selected constraint disappears before we take the lock, we re-read the
    catalog and retry; we never validate a constraint we did not select.

    Legitimate writes keep committing during the scan. If orphan rows remain,
    validation fails with 409 and write protection stays ON (the constraint
    remains NOT VALID); fix the orphans and call this again.
    """
    for attempt in range(3):
        with connect() as conn:
            candidates = find_equivalent_fks(conn)
            if not candidates:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "no equivalent foreign key exists; "
                        "POST /foreign-key/enable first"
                    ),
                )
            chosen = candidates[0]
            try:
                # Blocks concurrent RENAME/DROP/REPLACE from here on, while
                # leaving INSERT/UPDATE/DELETE unaffected.
                conn.execute(LOCK_FOR_VALIDATE_SQL)
                current = conn.execute(
                    EQUIVALENT_FK_BY_OID_SQL, {"oid": chosen["oid"]}
                ).fetchone()
                if current is None:
                    # Renamed *and* replaced between SELECT and LOCK: our
                    # selection is stale, start over instead of guessing.
                    conn.rollback()
                    continue
                if current[2]:
                    conn.rollback()
                    return {
                        "changed": False,
                        **_fk_row_to_status(current),
                        "equivalent_constraints": [
                            f["constraint"] for f in candidates
                        ],
                    }
                conn.execute(validate_fk_sql(current[1]))
                validated = conn.execute(
                    EQUIVALENT_FK_BY_OID_SQL, {"oid": chosen["oid"]}
                ).fetchone()
                conn.commit()
                return {
                    "changed": True,
                    **_fk_row_to_status(validated or current),
                    "equivalent_constraints": [
                        f["constraint"] for f in candidates
                    ],
                }
            except ForeignKeyViolation as exc:
                conn.rollback()
                with connect() as probe:
                    detail = {
                        "message": (
                            "validation failed: orphan inspection rows "
                            "remain; write protection stays ON"
                        ),
                        "postgres_error": exc.diag.message_primary,
                        "constraint": chosen["constraint"],
                        "orphan_count": orphan_count(probe),
                        "orphan_sample": orphan_rows(probe, 5),
                        "write_protection": (
                            "enforced (constraint remains NOT VALID)"
                        ),
                    }
                # jsonable_encoder: detail contains datetimes, and the default
                # HTTPException handler serializes with plain json.dumps
                raise HTTPException(
                    status_code=409, detail=jsonable_encoder(detail)
                )
    raise HTTPException(
        status_code=409,
        detail=(
            "the selected foreign key kept changing while validation tried "
            "to lock it; re-check /foreign-key/status and retry"
        ),
    )


# ---------------------------------------------------------------------------
# inspections
# ---------------------------------------------------------------------------

class InspectionIn(BaseModel):
    device_id: int
    note: str = ""


@app.post("/inspections", status_code=201, tags=["inspections"])
def create_inspection(body: InspectionIn):
    """Record an inspection. Once protection is enabled the database itself
    rejects rows pointing at a missing device (409)."""
    try:
        with connect() as conn:
            row = conn.execute(
                "INSERT INTO inspections (device_id, note) VALUES (%s, %s) "
                "RETURNING id, created_at",
                (body.device_id, body.note),
            ).fetchone()
    except ForeignKeyViolation:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"device {body.device_id} does not exist; "
                    "database refused the insert"
                )
            },
        )
    return {
        "id": row[0],
        "device_id": body.device_id,
        "note": body.note,
        "created_at": row[1],
    }


@app.get("/inspections", tags=["inspections"])
def list_inspections(limit: int = Query(50, ge=1, le=500)):
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, device_id, note, created_at FROM inspections "
            "ORDER BY id LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {"id": r[0], "device_id": r[1], "note": r[2], "created_at": r[3]}
        for r in rows
    ]


@app.get("/inspections/orphans", tags=["inspections"])
def list_orphans(limit: int = Query(100, ge=1, le=1000)):
    """Legacy rows whose device_id points at a device that no longer exists."""
    with connect() as conn:
        return {"count": orphan_count(conn), "rows": orphan_rows(conn, limit)}


class ReassignIn(BaseModel):
    expected_device_id: int = Field(
        ..., description="device_id the operator saw when listing orphans"
    )
    new_device_id: int = Field(
        ..., description="existing device to attach the record to"
    )


@app.post("/inspections/{inspection_id}/reassign", tags=["inspections"])
def reassign_inspection(inspection_id: int, body: ReassignIn):
    """Re-attach an orphan record to the correct device.

    Optimistic concurrency: the row is only updated if its device_id still
    equals ``expected_device_id``. If someone changed it in the meantime,
    returns 409 with the current value so the operator can re-check.
    """
    with connect() as conn:
        target = conn.execute(
            "SELECT id FROM devices WHERE id = %s", (body.new_device_id,)
        ).fetchone()
        if target is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"target device {body.new_device_id} does not exist"
                },
            )
        cur = conn.execute(
            "UPDATE inspections SET device_id = %s "
            "WHERE id = %s AND device_id = %s",
            (body.new_device_id, inspection_id, body.expected_device_id),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT device_id FROM inspections WHERE id = %s",
                (inspection_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404, detail=f"inspection {inspection_id} not found"
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "device_id changed since you read it; "
                        "re-list orphans and retry"
                    ),
                    "current_device_id": row[0],
                },
            )
    return {"id": inspection_id, "device_id": body.new_device_id}


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------

class DeviceIn(BaseModel):
    name: str
    location: str = ""


@app.post("/devices", status_code=201, tags=["devices"])
def create_device(body: DeviceIn):
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO devices (name, location) VALUES (%s, %s) "
            "RETURNING id, created_at",
            (body.name, body.location),
        ).fetchone()
    return {
        "id": row[0],
        "name": body.name,
        "location": body.location,
        "created_at": row[1],
    }


@app.get("/devices", tags=["devices"])
def list_devices():
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, location, created_at FROM devices ORDER BY id"
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "location": r[2], "created_at": r[3]}
        for r in rows
    ]


@app.delete("/devices/{device_id}", tags=["devices"])
def delete_device(device_id: int):
    """Once the FK exists, the database refuses to delete a device that is
    still referenced by inspections (409)."""
    try:
        with connect() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id = %s", (device_id,))
            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=404, detail=f"device {device_id} not found"
                )
    except ForeignKeyViolation:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"device {device_id} is still referenced by inspections; "
                    "database refused the delete"
                )
            },
        )
    return {"deleted": device_id}
