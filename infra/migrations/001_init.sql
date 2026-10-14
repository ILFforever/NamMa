-- Schema from CLAUDE.md §5. Timestamps are timestamptz; ThaiWater times are Asia/Bangkok.

CREATE TABLE stations (
    id                  INTEGER PRIMARY KEY,          -- ThaiWater station.id
    oldcode             TEXT,                         -- e.g. C.35, CPY011
    name_th             TEXT,
    name_en             TEXT,
    lat                 DOUBLE PRECISION,
    lon                 DOUBLE PRECISION,
    basin               TEXT,
    river               TEXT,
    province            TEXT,
    amphoe              TEXT,
    tambon              TEXT,
    left_bank           DOUBLE PRECISION,
    right_bank          DOUBLE PRECISION,
    min_bank            DOUBLE PRECISION,
    ground_level        DOUBLE PRECISION,
    critical_level_msl  DOUBLE PRECISION,
    agency              TEXT,
    is_key_station      BOOLEAN,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE station_edges (
    upstream_id          INTEGER NOT NULL REFERENCES stations(id),
    downstream_id        INTEGER NOT NULL REFERENCES stations(id),
    approx_travel_hours  DOUBLE PRECISION,             -- NULL until estimated from history
    PRIMARY KEY (upstream_id, downstream_id)
);

CREATE TABLE waterlevel_readings (
    station_id       INTEGER NOT NULL REFERENCES stations(id),
    ts               TIMESTAMPTZ NOT NULL,
    level_msl        DOUBLE PRECISION,
    diff_wl_bank     DOUBLE PRECISION,               -- signed: + above bank, - below
    situation_level  SMALLINT,
    discharge        DOUBLE PRECISION,               -- m3/s, RID stations only
    PRIMARY KEY (station_id, ts)
);

CREATE TABLE rain_stations (
    id        INTEGER PRIMARY KEY,                    -- separate id space from stations
    oldcode   TEXT,
    name_th   TEXT,
    lat       DOUBLE PRECISION,
    lon       DOUBLE PRECISION,
    province  TEXT,
    basin     TEXT
);

CREATE TABLE rain_readings (
    station_id  INTEGER NOT NULL REFERENCES rain_stations(id),
    ts          TIMESTAMPTZ NOT NULL,
    rain_1h     DOUBLE PRECISION,
    rain_24h    DOUBLE PRECISION,
    PRIMARY KEY (station_id, ts)
);

CREATE TABLE station_risk (
    station_id       INTEGER PRIMARY KEY REFERENCES stations(id),
    computed_at      TIMESTAMPTZ NOT NULL,
    level            DOUBLE PRECISION,
    trend_cm_per_hr  DOUBLE PRECISION,
    hours_to_bank    DOUBLE PRECISION,
    risk_tier        TEXT NOT NULL CHECK (risk_tier IN ('normal', 'watch', 'warning', 'danger')),
    forecast_json    JSONB
);

CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    line_user_id  TEXT UNIQUE,
    display_name  TEXT,
    language      TEXT NOT NULL DEFAULT 'th',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_locations (
    id       BIGSERIAL PRIMARY KEY,
    user_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,
    lat      DOUBLE PRECISION NOT NULL,
    lon      DOUBLE PRECISION NOT NULL,
    notes    TEXT
);

CREATE TABLE user_memory (
    user_id      BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    household    JSONB NOT NULL DEFAULT '{}',
    house        JSONB NOT NULL DEFAULT '{}',
    assets       JSONB NOT NULL DEFAULT '{}',
    needs        JSONB NOT NULL DEFAULT '{}',
    history      JSONB NOT NULL DEFAULT '{}',
    preferences  JSONB NOT NULL DEFAULT '{}',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE consents (
    user_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,                           -- 'sensitive_memory', 'alerts'
    granted  BOOLEAN NOT NULL,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX consents_user_kind ON consents (user_id, kind, ts DESC);

CREATE TABLE messages (
    id       BIGSERIAL PRIMARY KEY,
    user_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel  TEXT NOT NULL,                           -- 'line', 'web'
    role     TEXT NOT NULL,                           -- 'user', 'assistant'
    content  TEXT NOT NULL,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX messages_user_ts ON messages (user_id, ts DESC);

CREATE TABLE conversation_summary (
    user_id     BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    summary     TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE alerts_sent (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    location_id  BIGINT NOT NULL REFERENCES user_locations(id) ON DELETE CASCADE,
    station_id   INTEGER NOT NULL REFERENCES stations(id),
    risk_tier    TEXT NOT NULL,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX alerts_sent_location_ts ON alerts_sent (location_id, ts DESC);

CREATE TABLE rescue_tickets (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT REFERENCES users(id) ON DELETE SET NULL,
    raw_text      TEXT NOT NULL,
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    people_count  INTEGER,
    vulnerable    JSONB NOT NULL DEFAULT '[]',
    needs         JSONB NOT NULL DEFAULT '[]',
    urgency       TEXT,
    status        TEXT NOT NULL DEFAULT 'open',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE shelters (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT NOT NULL,
    lat               DOUBLE PRECISION NOT NULL,
    lon               DOUBLE PRECISION NOT NULL,
    capacity          INTEGER,
    accepts_patients  BOOLEAN NOT NULL DEFAULT false,
    accepts_pets      BOOLEAN NOT NULL DEFAULT false,
    phone             TEXT
);
