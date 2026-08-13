ALTER TABLE pull_requests
    ADD COLUMN github_updated_at timestamptz,
    ADD COLUMN github_delivery_received_at timestamptz,
    ADD COLUMN github_delivery_id varchar(128);

ALTER TABLE runs
    DROP CONSTRAINT runs_commit_unique,
    ADD COLUMN generation integer NOT NULL DEFAULT 1,
    ADD CONSTRAINT runs_generation_positive CHECK (generation > 0),
    ADD CONSTRAINT runs_commit_generation_unique
        UNIQUE (pull_request_id, head_sha, generation);

CREATE TABLE github_webhook_deliveries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    delivery_id varchar(128) NOT NULL,
    event_type text NOT NULL,
    action text NOT NULL,
    installation_id bigint NOT NULL,
    repository_github_id bigint NOT NULL,
    pull_request_number integer NOT NULL,
    head_sha varchar(40) NOT NULL,
    before_sha varchar(40),
    after_sha varchar(40),
    pull_request_updated_at timestamptz NOT NULL,
    command_public_id varchar(26),
    received_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT github_deliveries_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT github_deliveries_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT github_deliveries_command_id_ulid CHECK (
        command_public_id IS NULL
        OR command_public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT github_deliveries_event_known CHECK (event_type = 'pull_request'),
    CONSTRAINT github_deliveries_action_known CHECK (
        action IN ('opened', 'reopened', 'synchronize', 'closed')
    ),
    CONSTRAINT github_deliveries_installation_positive CHECK (installation_id > 0),
    CONSTRAINT github_deliveries_repository_positive CHECK (repository_github_id > 0),
    CONSTRAINT github_deliveries_pr_positive CHECK (pull_request_number > 0),
    CONSTRAINT github_deliveries_head_sha_format CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT github_deliveries_before_sha_format CHECK (
        before_sha IS NULL OR before_sha ~ '^[0-9a-f]{40}$'
    ),
    CONSTRAINT github_deliveries_after_sha_format CHECK (
        after_sha IS NULL OR after_sha ~ '^[0-9a-f]{40}$'
    ),
    CONSTRAINT github_deliveries_owner_delivery_unique UNIQUE (owner_id, delivery_id),
    CONSTRAINT github_deliveries_owner_internal_unique UNIQUE (owner_id, id)
);

CREATE INDEX github_deliveries_owner_received_idx
    ON github_webhook_deliveries (owner_id, received_at DESC);
