-- Shelters from DDPM's live shelter system (ingest/shelters.py), replacing the hand-made sample list.
-- accepts_bedridden is three-valued: true / false / NULL = DDPM has no answer (most rows), so "call first".
-- No coordinator names, emails or phones are stored (personal data); people are pointed to 1784.
DELETE FROM shelters WHERE notes LIKE 'DEMO:%';
ALTER TABLE shelters DROP CONSTRAINT shelters_name_key;     -- real names repeat ("ศาลาประชาคม")
ALTER TABLE shelters
    ADD COLUMN source            TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'ddpm')),
    ADD COLUMN source_code       TEXT,                       -- DDPM shelter_code
    ADD COLUMN is_open           BOOLEAN,                    -- DDPM is_active: open for admission now
    ADD COLUMN current_residents INTEGER,
    ADD COLUMN accepts_bedridden BOOLEAN,
    ADD COLUMN accepts_elderly   BOOLEAN,
    ADD COLUMN accepts_disabled  BOOLEAN,
    ADD COLUMN province          TEXT,
    ADD COLUMN district          TEXT,
    ADD COLUMN source_updated_at TIMESTAMPTZ,
    ADD COLUMN imported_at       TIMESTAMPTZ;
CREATE UNIQUE INDEX shelters_source_code ON shelters (source, source_code) WHERE source_code IS NOT NULL;
