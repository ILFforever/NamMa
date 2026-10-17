-- Web API contract (docs/api.md): ticket fields + enums, audit trail, responder role, web sessions,
-- corridor columns for the map.

-- Rescue tickets ---------------------------------------------------------------------------------
ALTER TABLE rescue_tickets
    ADD COLUMN place_label   TEXT,
    ADD COLUMN contact_phone TEXT,
    ADD COLUMN channel       TEXT NOT NULL DEFAULT 'line',
    ADD COLUMN assignee      TEXT,
    ADD COLUMN notes         TEXT;

-- Old status names → contract names. 'unconfirmed' stays: a triage ticket the person has not
-- confirmed yet. It is internal and never shown to responders.
UPDATE rescue_tickets SET status = 'acknowledged' WHERE status = 'assigned';
UPDATE rescue_tickets SET status = 'resolved' WHERE status = 'done';
ALTER TABLE rescue_tickets ADD CONSTRAINT rescue_tickets_status_check
    CHECK (status IN ('unconfirmed', 'open', 'acknowledged', 'in_progress', 'resolved', 'cancelled'));

UPDATE rescue_tickets SET urgency = 'high' WHERE urgency IS NULL OR urgency NOT IN ('critical', 'high', 'normal');
ALTER TABLE rescue_tickets ALTER COLUMN urgency SET DEFAULT 'high';
ALTER TABLE rescue_tickets ALTER COLUMN urgency SET NOT NULL;
ALTER TABLE rescue_tickets ADD CONSTRAINT rescue_tickets_urgency_check CHECK (urgency IN ('critical', 'high', 'normal'));
ALTER TABLE rescue_tickets ADD CONSTRAINT rescue_tickets_channel_check CHECK (channel IN ('line', 'web'));

CREATE INDEX rescue_tickets_updated ON rescue_tickets (updated_at DESC);

-- Who viewed or changed a ticket (tickets hold PDPA-sensitive health data).
CREATE TABLE ticket_events (
    id         BIGSERIAL PRIMARY KEY,
    ticket_id  BIGINT NOT NULL REFERENCES rescue_tickets(id) ON DELETE CASCADE,
    actor      TEXT NOT NULL,              -- users.id of the responder, as text, or 'system'
    action     TEXT NOT NULL,              -- view | status | assign | note
    detail     JSONB,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ticket_events_ticket ON ticket_events (ticket_id, ts DESC);

-- Users and web sessions ---------------------------------------------------------------------------
ALTER TABLE users
    ADD COLUMN role        TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'responder')),
    ADD COLUMN picture_url TEXT;

-- Only a hash of the bearer token is stored.
CREATE TABLE sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX sessions_user ON sessions (user_id);

-- Stations: position on the demo corridor (from data/corridor_stations.csv via common.seed) ------
ALTER TABLE stations
    ADD COLUMN stem_order INTEGER,
    ADD COLUMN role       TEXT CHECK (role IN ('main', 'branch', 'tributary'));
