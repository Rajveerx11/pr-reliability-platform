CREATE TABLE github_installations (
    owner_id varchar(26) NOT NULL CHECK (owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'),
    installation_id bigint NOT NULL CHECK (installation_id > 0),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'active', 'suspended', 'deleted')),
    revision bigint NOT NULL DEFAULT 0,
    last_sync_at timestamptz,
    last_webhook_at timestamptz,
    PRIMARY KEY (owner_id, installation_id)
);

ALTER TABLE repositories
    ADD COLUMN installation_id bigint,
    ADD COLUMN access_state text NOT NULL DEFAULT 'pending'
        CHECK (access_state IN ('pending', 'active', 'removed')),
    ADD COLUMN default_branch text,
    ADD COLUMN enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN enabled_branches text[] NOT NULL DEFAULT '{}',
    ADD COLUMN token_budget integer NOT NULL DEFAULT 100000 CHECK (token_budget > 0),
    ADD COLUMN cost_budget_usd_micros bigint NOT NULL DEFAULT 1000000
        CHECK (cost_budget_usd_micros >= 0),
    ADD COLUMN verification_profile text NOT NULL DEFAULT 'default'
        CHECK (verification_profile = 'default'),
    ADD COLUMN last_sync_at timestamptz,
    ADD COLUMN last_webhook_at timestamptz,
    ADD COLUMN last_review_run_at timestamptz,
    ADD CONSTRAINT repositories_installation_fk FOREIGN KEY (owner_id, installation_id)
        REFERENCES github_installations (owner_id, installation_id);

ALTER TABLE runs
    ADD COLUMN base_branch text,
    ADD COLUMN verification_profile text NOT NULL DEFAULT 'default'
        CHECK (verification_profile = 'default');

CREATE TABLE repository_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id varchar(26) NOT NULL,
    installation_id bigint NOT NULL,
    event_key text NOT NULL CHECK (length(event_key) BETWEEN 1 AND 160),
    event_type text NOT NULL,
    event_data jsonb NOT NULL CHECK (jsonb_typeof(event_data) = 'object'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, event_key),
    FOREIGN KEY (owner_id, installation_id)
        REFERENCES github_installations (owner_id, installation_id)
);
CREATE TRIGGER repository_events_append_only
BEFORE UPDATE OR DELETE ON repository_events
FOR EACH ROW EXECUTE FUNCTION reject_run_event_mutation();
CREATE TRIGGER repository_events_reject_truncate
BEFORE TRUNCATE ON repository_events
FOR EACH STATEMENT EXECUTE FUNCTION reject_run_event_mutation();
CREATE INDEX repositories_owner_inventory_idx ON repositories (owner_id, public_id);
