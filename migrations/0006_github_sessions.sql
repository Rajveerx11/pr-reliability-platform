CREATE TABLE github_users (
    owner_id text NOT NULL,
    github_user_id bigint NOT NULL CHECK (github_user_id > 0),
    actor_id text NOT NULL CHECK (actor_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'),
    login text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (owner_id, github_user_id),
    UNIQUE (owner_id, actor_id)
);

CREATE TABLE browser_sessions (
    token_hash text PRIMARY KEY,
    owner_id text NOT NULL,
    github_user_id bigint NOT NULL,
    encrypted_token text NOT NULL,
    csrf_token text NOT NULL,
    expires_at timestamptz NOT NULL,
    FOREIGN KEY (owner_id, github_user_id)
        REFERENCES github_users (owner_id, github_user_id)
);
CREATE INDEX browser_sessions_user ON browser_sessions (owner_id, github_user_id);
CREATE INDEX browser_sessions_expiry ON browser_sessions (expires_at);

CREATE TABLE github_login_attempts (
    state_hash text PRIMARY KEY,
    owner_id text NOT NULL,
    browser_hash text NOT NULL,
    verifier text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX github_login_attempts_expiry ON github_login_attempts (expires_at);

CREATE TABLE github_login_limits (
    client_hash text PRIMARY KEY,
    attempts integer NOT NULL CHECK (attempts > 0),
    expires_at timestamptz NOT NULL
);
CREATE INDEX github_login_limits_expiry ON github_login_limits (expires_at);
