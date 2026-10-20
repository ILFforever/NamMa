-- Our own record of GISTDA's live flood layers (copy plan phase 4). flood_cells holds only the latest 3-day and
-- 7-day layer and GISTDA keeps no history of them, so every stored layer is also copied here, one copy per period
-- per day (Bangkok date): a later layer the same day replaces that day's rows. Kept for good.
-- Names and H3 parents are left out (they come from the codes and from h3), as are GISTDA's other attributes; the
-- raw pages under var/raw/gistda/flood/<period>/<time>/ keep everything for GISTDA_SNAPSHOT_KEEP_DAYS.
CREATE TABLE flood_cells_history (
    day          DATE NOT NULL,
    period       TEXT NOT NULL CHECK (period IN ('3days', '7days')),
    h3           TEXT NOT NULL,
    fetch_id     BIGINT NOT NULL REFERENCES flood_fetches (id),
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    area_km2     DOUBLE PRECISION NOT NULL,
    pv_code      TEXT,
    ap_code      TEXT,
    tb_code      TEXT,
    observed_at  TIMESTAMPTZ,
    buildings    DOUBLE PRECISION,
    population   DOUBLE PRECISION,
    schools      DOUBLE PRECISION,
    hospitals    DOUBLE PRECISION,
    PRIMARY KEY (day, period, h3)
);
