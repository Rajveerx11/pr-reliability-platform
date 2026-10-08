-- Issue #42, DEC-014: ciphertext only; product events hold opaque references.
CREATE TABLE verification_artifacts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id text NOT NULL,
    run_id bigint NOT NULL,
    reference text NOT NULL CHECK (reference ~ '^ev_[0-9a-f]{64}$'),
    check_name text NOT NULL CHECK (check_name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    ciphertext bytea,
    plaintext_bytes integer NOT NULL CHECK (plaintext_bytes BETWEEN 1 AND 10485760),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    expired_at timestamptz,
    expiry_event_id text NOT NULL CHECK (expiry_event_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'),
    UNIQUE (owner_id, reference),
    UNIQUE (owner_id, run_id, check_name),
    FOREIGN KEY (owner_id, run_id) REFERENCES runs (owner_id, id),
    CHECK (expires_at > created_at),
    CHECK ((ciphertext IS NULL) = (expired_at IS NOT NULL)),
    CHECK (octet_length(ciphertext) <= 13982000)
);
CREATE INDEX verification_artifacts_expiry_idx
    ON verification_artifacts (expires_at) WHERE expired_at IS NULL;
