-- LINE Login in the browser (api/auth.py, docs/api.md → LINE web login). One row per trip to LINE: the state we
-- sent (hashed; the browser keeps it in a cookie), then, once LINE sent the person back, the account it signed in
-- and a one-time claim (hashed) the page exchanges for a session. Rows live 10 minutes; older ones are deleted.
CREATE TABLE line_logins (
    state_hash   TEXT PRIMARY KEY,
    nonce        TEXT NOT NULL,
    intent       TEXT NOT NULL CHECK (intent IN ('login', 'link')),
    link_user_id INT REFERENCES users(id) ON DELETE CASCADE,     -- intent 'link': the signed-in account
    chat_session TEXT,                                           -- intent 'login': the anonymous web chat to fold in
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    claim_hash   TEXT UNIQUE,
    user_id      INT REFERENCES users(id) ON DELETE CASCADE,
    merged_chat  BOOLEAN NOT NULL DEFAULT false
);
