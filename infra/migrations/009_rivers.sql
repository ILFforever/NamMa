-- Every station's place on a river, for GET /api/rivers (docs/rivers-api.md). Written by ingest.rivers after each
-- ingest; NULL river_key = not on any river chart (no river name, no position or no usable bank).
ALTER TABLE stations
    ADD COLUMN IF NOT EXISTS river_key     TEXT,
    ADD COLUMN IF NOT EXISTS river_order   INTEGER,                     -- 1 = furthest upstream; NULL with same_place_as
    ADD COLUMN IF NOT EXISTS same_place_as INTEGER REFERENCES stations(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS stations_river ON stations (river_key, river_order);
