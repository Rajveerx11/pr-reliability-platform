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
    CONSTRAINT github_deliveries_owner_delivery_unique UNIQUE (owner_id, delivery_id),
    CONSTRAINT github_deliveries_owner_internal_unique UNIQUE (owner_id, id)
);

CREATE INDEX github_deliveries_owner_received_idx
    ON github_webhook_deliveries (owner_id, received_at DESC);
