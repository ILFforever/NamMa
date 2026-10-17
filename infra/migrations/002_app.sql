-- App-side additions for day 2-4: consent-pending facts, alert bookkeeping, shelter notes.

ALTER TABLE shelters ADD COLUMN notes TEXT;
ALTER TABLE shelters ADD CONSTRAINT shelters_name_key UNIQUE (name);

-- Rows with pushed = false record a tier drop, so a later rise alerts again.
ALTER TABLE alerts_sent ADD COLUMN pushed BOOLEAN NOT NULL DEFAULT true;

-- Sensitive facts waiting for PDPA consent. Usable for the current conversation only.
CREATE TABLE pending_facts (
    user_id  BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    facts    JSONB NOT NULL,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX waterlevel_readings_ts ON waterlevel_readings (ts DESC);
CREATE INDEX rescue_tickets_status ON rescue_tickets (status, created_at DESC);
