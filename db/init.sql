-- Sample "inspection" database.
--
-- Reproduces the production situation: the collector writes inspections
-- straight into PostgreSQL, there is NO foreign key yet, and some old
-- inspection rows point at devices that were decommissioned and deleted
-- (orphan rows).

BEGIN;

CREATE TABLE devices (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT NOT NULL,
    location   TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE inspections (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id  BIGINT,               -- no FK yet: that is the point
    note       TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_inspections_device ON inspections (device_id);

-- Devices that are still in service (ids 1..5).
INSERT INTO devices (name, location) VALUES
    ('pump-01',   'plant-a'),
    ('pump-02',   'plant-a'),
    ('valve-01',  'plant-b'),
    ('valve-02',  'plant-b'),
    ('sensor-01', 'plant-c');

-- Devices that will be decommissioned and deleted below (ids 6..8).
INSERT INTO devices (name, location) VALUES
    ('old-pump-09',   'plant-a'),
    ('old-valve-07',  'plant-b'),
    ('old-sensor-03', 'plant-c');

-- Historical inspections: some reference healthy devices, some reference
-- the soon-to-be-deleted ones.
INSERT INTO inspections (device_id, note) VALUES
    (1, 'routine check'),
    (2, 'routine check'),
    (3, 'routine check'),
    (4, 'routine check'),
    (5, 'routine check'),
    (6, 'pre-decommission reading'),
    (6, 'pre-decommission reading'),
    (7, 'pre-decommission reading'),
    (8, 'pre-decommission reading');

-- No FK yet, so this delete succeeds and leaves orphan inspections behind
-- (device_id 6, 7, 8 no longer exist).
DELETE FROM devices WHERE id IN (6, 7, 8);

COMMIT;
