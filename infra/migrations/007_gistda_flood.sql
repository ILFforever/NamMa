-- GISTDA satellite flood extent (ingest/gistda_flood.py, CLAUDE.md §4.4): one row per flooded H3 cell in GISTDA's
-- latest 3-day and 7-day layers. A fetch replaces its period's rows in one transaction; flood_fetches keeps one row
-- per fetch, so the totals have a history even though the cells don't.
CREATE TABLE flood_cells (
    period       TEXT NOT NULL CHECK (period IN ('3days', '7days')),
    h3           TEXT NOT NULL,              -- the cell as GISTDA sent it (res 9, ~0.1 km²)
    h3_r5        TEXT NOT NULL,              -- parents for zoomed-out maps: ~250 / 36 / 5 / 0.7 km²
    h3_r6        TEXT NOT NULL,
    h3_r7        TEXT NOT NULL,
    h3_r8        TEXT NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,  -- cell centre
    lon          DOUBLE PRECISION NOT NULL,
    area_km2     DOUBLE PRECISION NOT NULL,  -- flooded area in the cell: GISTDA's figure, else the whole cell
    pv_code      TEXT,                       -- GISTDA area codes (pv_idn 2 digits, ap_idn 4, tb_idn 6) when sent
    ap_code      TEXT,
    tb_code      TEXT,
    province     TEXT,                       -- names without the จังหวัด/อำเภอ/ตำบล prefix
    amphoe       TEXT,
    tambon       TEXT,
    observed_at  TIMESTAMPTZ,                -- the date GISTDA gives the cell, if any
    props        JSONB NOT NULL DEFAULT '{}', -- GISTDA's other attributes as sent (people, buildings, crops…)
    PRIMARY KEY (period, h3)
);
CREATE INDEX flood_cells_point ON flood_cells (period, lat, lon);

CREATE TABLE flood_fetches (
    id             BIGSERIAL PRIMARY KEY,
    period         TEXT NOT NULL CHECK (period IN ('3days', '7days')),
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    cells          INTEGER NOT NULL,
    area_km2       DOUBLE PRECISION NOT NULL,
    observed_from  TIMESTAMPTZ,
    observed_to    TIMESTAMPTZ,
    fingerprint    TEXT,                     -- of the first page: an unchanged layer is not downloaded again
    area_unit      TEXT,                     -- how GISTDA's area figure was read: km2 | rai | m2 | cell
    by_province    JSONB NOT NULL DEFAULT '{}'   -- province → km², for a history of totals
);
CREATE INDEX flood_fetches_period ON flood_fetches (period, fetched_at DESC);
