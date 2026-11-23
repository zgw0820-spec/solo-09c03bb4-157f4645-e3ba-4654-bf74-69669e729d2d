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
    FK_NAME,
    ORPHAN_WHERE,
    STATUS_SQL,
    VALIDATE_FK_SQL,
    connect,
)

app = FastAPI(
    title="Online FK migration API",
    version="1.0.0",
    description=(
        "Adds the `inspections.device_id -> devices.id` foreign key online, "
        "while the collector keeps writing directly to PostgreSQL. "
        "Enable protection first, clean up orphan rows, then validate."
    ),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def read_fk_status() -> dict[str, Any]:
    """Actual constraint state from the PostgreSQL catalog.

    The API keeps no in-memory state, so this is correct across restarts.
    """
    with connect() as conn:
        row = conn.execute(STATUS_SQL, {"name": FK_NAME}).fetchone()
    if row is None:
        return {
            "constraint": FK_NAME,
            "exists": False,
            "enforced": False,
            "validated": False,
            "definition": None,
        }
    name, validated, definition = row
    return {
        "constraint": name,
        "exists": True,
        "enforced": True,
        "validated": validated,
        "definition": definition,
    }


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
    """Step 1: add the FK as NOT VALID.

    Brief catalog lock only, no table scan. From this moment the database
    rejects new/updated inspections pointing at missing devices and refuses
    to delete devices that are still referenced -- even for clients that
    bypass this API and write to PostgreSQL directly.
    """
    before = read_fk_status()
    if before["exists"]:
        return {"changed": False, **before}
    try:
        with connect(autocommit=True) as conn:
            conn.execute(ADD_FK_SQL)
    except DuplicateObject:
        pass  # concurrent enabler won the race; state is what matters
    return {"changed": True, **read_fk_status()}


@app.post("/foreign-key/validate", tags=["foreign-key"])
def fk_validate():
    """Step 3: validate the FK against the whole table.

    Runs under a SHARE UPDATE EXCLUSIVE lock: legitimate reads and writes
    keep committing during the scan. If orphan rows remain, validation
    fails with 409 and write protection stays ON (the constraint remains
    NOT VALID); fix the orphans and call this again.
    """
    status = read_fk_status()
    if not status["exists"]:
        raise HTTPException(
            status_code=409,
            detail="foreign key not enabled yet; POST /foreign-key/enable first",
        )
    if status["validated"]:
        return {"changed": False, **status}
    try:
        with connect(autocommit=True) as conn:
            conn.execute(VALIDATE_FK_SQL)
    except ForeignKeyViolation as exc:
        with connect() as conn:
            detail = {
                "message": (
                    "validation failed: orphan inspection rows remain; "
                    "write protection stays ON"
                ),
                "postgres_error": exc.diag.message_primary,
                "orphan_count": orphan_count(conn),
                "orphan_sample": orphan_rows(conn, 5),
                "write_protection": "enforced (constraint remains NOT VALID)",
            }
        # jsonable_encoder: detail contains datetimes, and the default
        # HTTPException handler serializes with plain json.dumps
        raise HTTPException(status_code=409, detail=jsonable_encoder(detail))
    return {"changed": True, **read_fk_status()}


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
