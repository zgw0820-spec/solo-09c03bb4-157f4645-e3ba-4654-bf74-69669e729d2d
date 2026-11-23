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
from psycopg import sql

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ops"
)

FK_NAME = "fk_inspections_device"

# The only foreign-key shape this service manages.  A real-world database may
# contain manually added constraints, so we identify ours by *definition*, not
# by name:
#
#   inspections.device_id -> devices.id
#   single column, NOT DEFERRABLE, ON UPDATE NO ACTION, ON DELETE NO ACTION
#
# Everything else (another target table, extra columns, DEFERRABLE, RESTRICT,
# CASCADE, ...) is deliberately ignored.
_EQUIVALENT_FK_PREDICATE = """
    c.conrelid = 'inspections'::regclass
    AND c.confrelid = 'devices'::regclass
    AND c.contype = 'f'
    AND c.conparentid = 0
    AND NOT c.condeferrable
    AND NOT c.condeferred
    AND c.confupdtype = 'a'
    AND c.confdeltype = 'a'
    AND cardinality(c.conkey) = 1
    AND cardinality(c.confkey) = 1
    AND c.conkey[1] = (
        SELECT a.attnum
        FROM pg_attribute a
        WHERE a.attrelid = 'inspections'::regclass
          AND a.attname = 'device_id'
          AND NOT a.attisdropped
    )
    AND c.confkey[1] = (
        SELECT a.attnum
        FROM pg_attribute a
        WHERE a.attrelid = 'devices'::regclass
          AND a.attname = 'id'
          AND NOT a.attisdropped
    )
"""

# Validated constraints first: when synonyms exist, report the strongest one.
EQUIVALENT_FKS_SQL = f"""
SELECT c.oid, c.conname, c.convalidated, pg_get_constraintdef(c.oid)
FROM pg_constraint c
WHERE {_EQUIVALENT_FK_PREDICATE}
ORDER BY c.convalidated DESC, c.conname
"""

# Look a selected constraint up by identity (OID), not by name: it may have
# been renamed while we were waiting for the validation lock.
EQUIVALENT_FK_BY_OID_SQL = f"""
SELECT c.oid, c.conname, c.convalidated, pg_get_constraintdef(c.oid)
FROM pg_constraint c
WHERE {_EQUIVALENT_FK_PREDICATE}
  AND c.oid = %(oid)s
"""

# Used only to explain a 409 when the default name is already taken.
CONSTRAINT_BY_NAME_SQL = """
SELECT conname, contype, convalidated, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'inspections'::regclass
  AND conname = %(name)s
"""

ADD_FK_SQL = (
    "ALTER TABLE inspections "
    f"ADD CONSTRAINT {FK_NAME} "
    "FOREIGN KEY (device_id) REFERENCES devices (id) NOT VALID"
)

# SHARE UPDATE EXCLUSIVE is compatible with ROW EXCLUSIVE (ordinary INSERT /
# UPDATE from the collector), but conflicts with the ACCESS EXCLUSIVE lock
# needed to rename, drop or replace a constraint.  Holding it from "re-read
# the selected constraint" through "VALIDATE CONSTRAINT" guarantees we can
# never validate a constraint other than the one we selected.
LOCK_FOR_VALIDATE_SQL = "LOCK TABLE inspections IN SHARE UPDATE EXCLUSIVE MODE"


def validate_fk_sql(name: str) -> sql.Composable:
    """VALIDATE a particular constraint, quoting its (possibly manual) name."""
    return sql.SQL("ALTER TABLE inspections VALIDATE CONSTRAINT {}").format(
        sql.Identifier(name)
    )


# Orphan = inspection whose device_id is set but no longer exists.
ORPHAN_WHERE = """
FROM inspections i
LEFT JOIN devices d ON d.id = i.device_id
WHERE i.device_id IS NOT NULL AND d.id IS NULL
"""


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, autocommit=autocommit)
