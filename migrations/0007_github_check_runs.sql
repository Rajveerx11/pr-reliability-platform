ALTER TABLE runs
    ADD CONSTRAINT runs_owner_pr_head_identity_unique
        UNIQUE (owner_id, id, pull_request_id, head_sha);

CREATE TABLE github_check_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    pull_request_id bigint NOT NULL,
    current_run_id bigint NOT NULL,
    head_sha varchar(40) NOT NULL,
    check_name text NOT NULL,
    external_id text NOT NULL,
    remote_id bigint,
    status text NOT NULL DEFAULT 'queued',
    conclusion text,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT github_check_runs_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT github_check_runs_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT github_check_runs_head_sha_format CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT github_check_runs_name_present CHECK (length(btrim(check_name)) > 0),
    CONSTRAINT github_check_runs_external_id_present CHECK (length(btrim(external_id)) > 0),
    CONSTRAINT github_check_runs_remote_id_positive CHECK (remote_id IS NULL OR remote_id > 0),
    CONSTRAINT github_check_runs_status_known CHECK (
        status IN ('queued', 'in_progress', 'completed')
    ),
    CONSTRAINT github_check_runs_conclusion_known CHECK (
        conclusion IS NULL OR conclusion IN (
            'success', 'failure', 'action_required', 'cancelled', 'timed_out'
        )
    ),
    CONSTRAINT github_check_runs_terminal_shape CHECK (
        (status = 'completed' AND conclusion IS NOT NULL AND completed_at IS NOT NULL)
        OR (status <> 'completed' AND conclusion IS NULL AND completed_at IS NULL)
    ),
    CONSTRAINT github_check_runs_pull_request_fk FOREIGN KEY (owner_id, pull_request_id)
        REFERENCES pull_requests (owner_id, id),
    CONSTRAINT github_check_runs_run_fk FOREIGN KEY (
        owner_id, current_run_id, pull_request_id, head_sha
    ) REFERENCES runs (owner_id, id, pull_request_id, head_sha),
    CONSTRAINT github_check_runs_target_unique UNIQUE (
        pull_request_id, head_sha, check_name
    ),
    CONSTRAINT github_check_runs_external_unique UNIQUE (owner_id, external_id),
    CONSTRAINT github_check_runs_remote_unique UNIQUE (owner_id, remote_id),
    CONSTRAINT github_check_runs_owner_internal_unique UNIQUE (owner_id, id)
);

CREATE TABLE github_check_rerun_deliveries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    delivery_id varchar(128) NOT NULL,
    installation_id bigint NOT NULL,
    repository_github_id bigint NOT NULL,
    check_run_remote_id bigint NOT NULL,
    action text NOT NULL,
    head_sha varchar(40) NOT NULL,
    command_public_id varchar(26),
    received_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT github_check_rerun_deliveries_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT github_check_rerun_deliveries_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT github_check_rerun_deliveries_command_id_ulid CHECK (
        command_public_id IS NULL
        OR command_public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT github_check_rerun_deliveries_installation_positive CHECK (installation_id > 0),
    CONSTRAINT github_check_rerun_deliveries_repository_positive CHECK (
        repository_github_id > 0
    ),
    CONSTRAINT github_check_rerun_deliveries_remote_positive CHECK (check_run_remote_id > 0),
    CONSTRAINT github_check_rerun_deliveries_action_known CHECK (
        action IN ('rerequested', 'requested_action')
    ),
    CONSTRAINT github_check_rerun_deliveries_head_sha_format CHECK (
        head_sha ~ '^[0-9a-f]{40}$'
    ),
    CONSTRAINT github_check_rerun_deliveries_installation_fk FOREIGN KEY (
        owner_id, installation_id
    ) REFERENCES github_installations (owner_id, installation_id),
    CONSTRAINT github_check_rerun_deliveries_owner_delivery_unique UNIQUE (
        owner_id, delivery_id
    ),
    CONSTRAINT github_check_rerun_deliveries_owner_internal_unique UNIQUE (owner_id, id)
);

CREATE INDEX github_check_runs_owner_updated_idx
    ON github_check_runs (owner_id, updated_at DESC);
CREATE INDEX github_check_rerun_deliveries_owner_received_idx
    ON github_check_rerun_deliveries (owner_id, received_at DESC);
