-- Accounts with more than one way to sign in (docs/api.md → Auth). A person can sign in with LINE, with Google,
-- or with both linked to one account, so their places and memory follow them.
-- LINE stays in users.line_user_id: the webhook and alert pushes key on it. Other providers live here.
-- An account created by Google sign-in has line_user_id NULL until LINE is linked.
CREATE TABLE user_identities (
    provider    TEXT NOT NULL CHECK (provider IN ('google')),
    subject     TEXT NOT NULL,                    -- the provider's stable user id (Google: the ID token's "sub")
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email       TEXT,                             -- shown on the account page only; never used to match accounts
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, subject),
    UNIQUE (user_id, provider)                    -- one Google account per NamMa account
);
