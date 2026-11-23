"""Database access helpers and the SQL used by the online FK migration.

The whole migration relies on PostgreSQL's two-step online pattern:

1. ``ALTER TABLE ... ADD CONSTRAINT ... NOT VALID``
   Takes only a *brief* ACCESS EXCLUSIVE lock (catalog change, no table
   scan) and is immediately enforced for all new INSERT/UPDATE traffic --
   including the collector, which keeps writing directly to PostgreSQL.

2. ``ALTER TABLE ... VALIDATE CONSTRAINT``
   Scans the whole table under a SHARE UPDATE EXCLUSIVE lock, which does
   NOT block reads or writes, so legitimate writes keep committing while
   the scan runs.  Rows written during the scan are already checked by
   step 1, so the combination is safe.

Because the state lives in the PostgreSQL catalog (``pg_constraint``),
the API is stateless: after a restart it simply re-reads the catalog.
"""

from __future__ import annotations

import os

import psycopg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ops"
)

FK_NAME = "fk_inspections_device"

ADD_FK_SQL = (
    "ALTER TABLE inspections "
    f"ADD CONSTRAINT {FK_NAME} "
    "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID"
)

VALIDATE_FK_SQL = f"ALTER TABLE inspections VALIDATE CONSTRAINT {FK_NAME}"

# Actual FK state, straight from the catalog -> survives restarts.
STATUS_SQL = """
SELECT conname, convalidated, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'inspections'::regclass
  AND contype = 'f'
  AND conname = %(name)s
"""

# Orphan = inspection whose device_id is set but no longer exists.
ORPHAN_WHERE = """
FROM inspections i
LEFT JOIN devices d ON d.id = i.device_id
WHERE i.device_id IS NOT NULL AND d.id IS NULL
"""


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, autocommit=autocommit)
