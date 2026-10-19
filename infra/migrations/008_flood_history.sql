-- GISTDA flood history 2011–2024 (ingest/gistda_history.py, copy plan phases 1–2): our own copy of features/flood-freq,
-- so the chat answers "how often has this place flooded" without calling GISTDA. A province's build replaces its rows
-- in one transaction. Year masks: bit (year − 2011) is set when GISTDA's satellites saw water there that year.
CREATE TABLE flood_history_cells (
    pv_code   TEXT NOT NULL,                -- the province whose shapes put the cell here (a border cell can be in two)
    h3        TEXT NOT NULL,                -- res 9, ~0.1 km²: a cell whose centre lies in a flooded shape
    lat       DOUBLE PRECISION NOT NULL,    -- cell centre
    lon       DOUBLE PRECISION NOT NULL,
    years     INTEGER NOT NULL,             -- OR of the masks of every shape covering the cell
    freq      SMALLINT NOT NULL,            -- years flooded: the bits set in `years`
    ap_code   TEXT,
    tb_code   TEXT,
    PRIMARY KEY (pv_code, h3)
);
CREATE INDEX flood_history_cells_h3 ON flood_history_cells (h3);
CREATE INDEX flood_history_cells_point ON flood_history_cells (lat, lon);

CREATE TABLE flood_history_tambon (
    tb_code      TEXT PRIMARY KEY,          -- GISTDA tb_idn: province 2 + amphoe 2 + tambon 2 digits
    ap_code      TEXT,
    pv_code      TEXT NOT NULL,
    tambon       TEXT,                      -- names without the จังหวัด/อำเภอ/ตำบล prefix
    amphoe       TEXT,
    province     TEXT,
    years        INTEGER NOT NULL,          -- OR of its shapes' masks: any flooding in the sub-district that year
    freq         SMALLINT NOT NULL,
    shapes       INTEGER NOT NULL,
    rai_by_year  JSONB NOT NULL DEFAULT '{}'  -- "2011" → flooded rai, from its cells (so ~0.1 km² steps)
);
CREATE INDEX flood_history_tambon_pv ON flood_history_tambon (pv_code);

-- One row per province copied: the numbers phase 1 asks to write down, and phase 2's check against numberMatched.
CREATE TABLE flood_history_runs (
    pv_code         TEXT PRIMARY KEY,
    number_matched  INTEGER,                -- GISTDA's count for the province
    shapes          INTEGER NOT NULL,       -- shapes we read; equals number_matched for a complete copy
    pages           INTEGER NOT NULL,
    raw_bytes       BIGINT NOT NULL,        -- gzipped pages on disk
    download_s      REAL,                   -- NULL when every page was already on disk
    cells           INTEGER NOT NULL,
    tambons         INTEGER NOT NULL,
    built_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
