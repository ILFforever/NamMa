-- GISTDA's impact counts per flooded cell (ingest/gistda_flood.py IMPACT_KEYS), for the web flood tab's cell panel
-- and area rows. NULL = GISTDA sent no such figure: the UI then leaves it out, never shows 0.
ALTER TABLE flood_cells
    ADD COLUMN buildings  DOUBLE PRECISION,
    ADD COLUMN population DOUBLE PRECISION,
    ADD COLUMN schools    DOUBLE PRECISION,
    ADD COLUMN hospitals  DOUBLE PRECISION;
