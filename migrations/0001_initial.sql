CREATE TABLE repositories (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    github_repository_id bigint NOT NULL,
    full_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT repositories_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT repositories_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT repositories_github_id_positive CHECK (github_repository_id > 0),
    CONSTRAINT repositories_full_name_present CHECK (length(btrim(full_name)) > 0),
    CONSTRAINT repositories_owner_github_unique UNIQUE (owner_id, github_repository_id),
    CONSTRAINT repositories_owner_internal_unique UNIQUE (owner_id, id)
);

CREATE TABLE pull_requests (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    repository_id bigint NOT NULL,
    github_number integer NOT NULL,
    base_sha varchar(40) NOT NULL,
    head_sha varchar(40) NOT NULL,
    state text NOT NULL DEFAULT 'open',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pull_requests_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT pull_requests_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT pull_requests_number_positive CHECK (github_number > 0),
    CONSTRAINT pull_requests_base_sha_format CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT pull_requests_head_sha_format CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT pull_requests_state_known CHECK (state IN ('open', 'closed')),
    CONSTRAINT pull_requests_repository_fk FOREIGN KEY (owner_id, repository_id)
        REFERENCES repositories (owner_id, id),
    CONSTRAINT pull_requests_repository_number_unique UNIQUE (repository_id, github_number),
    CONSTRAINT pull_requests_owner_internal_unique UNIQUE (owner_id, id)
);

CREATE TABLE runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    pull_request_id bigint NOT NULL,
    base_sha varchar(40) NOT NULL,
    head_sha varchar(40) NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    token_budget integer NOT NULL,
    cost_budget_usd_micros bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT runs_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT runs_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT runs_base_sha_format CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT runs_head_sha_format CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT runs_state_known CHECK (
        state IN (
            'queued',
            'selecting_context',
            'analyzing',
            'verifying',
            'awaiting_approval',
            'published',
            'rejected',
            'failed',
            'cancelled'
        )
    ),
    CONSTRAINT runs_token_budget_positive CHECK (token_budget > 0),
    CONSTRAINT runs_cost_budget_nonnegative CHECK (cost_budget_usd_micros >= 0),
    CONSTRAINT runs_pull_request_fk FOREIGN KEY (owner_id, pull_request_id)
        REFERENCES pull_requests (owner_id, id),
    CONSTRAINT runs_commit_unique UNIQUE (pull_request_id, head_sha),
    CONSTRAINT runs_owner_internal_unique UNIQUE (owner_id, id),
    CONSTRAINT runs_owner_internal_head_unique UNIQUE (owner_id, id, head_sha)
);

CREATE TABLE findings (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    run_id bigint NOT NULL,
    finding_key text NOT NULL,
    category text NOT NULL,
    severity text NOT NULL,
    claim text NOT NULL,
    confidence double precision NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT findings_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT findings_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT findings_key_present CHECK (length(btrim(finding_key)) > 0),
    CONSTRAINT findings_category_present CHECK (length(btrim(category)) > 0),
    CONSTRAINT findings_severity_known CHECK (
        severity IN ('low', 'medium', 'high', 'critical')
    ),
    CONSTRAINT findings_claim_present CHECK (length(btrim(claim)) > 0),
    CONSTRAINT findings_confidence_range CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT findings_evidence_array CHECK (
        CASE
            WHEN jsonb_typeof(evidence) = 'array' THEN jsonb_array_length(evidence) > 0
            ELSE false
        END
    ),
    CONSTRAINT findings_run_fk FOREIGN KEY (owner_id, run_id)
        REFERENCES runs (owner_id, id),
    CONSTRAINT findings_run_key_unique UNIQUE (run_id, finding_key),
    CONSTRAINT findings_owner_run_internal_unique UNIQUE (owner_id, run_id, id)
);

CREATE TABLE approvals (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    run_id bigint NOT NULL,
    finding_id bigint NOT NULL,
    actor_id varchar(26) NOT NULL,
    decision text NOT NULL,
    reason text,
    head_sha varchar(40) NOT NULL,
    decided_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT approvals_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT approvals_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT approvals_actor_id_ulid CHECK (
        actor_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT approvals_decision_known CHECK (decision IN ('approved', 'rejected')),
    CONSTRAINT approvals_head_sha_format CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT approvals_run_fk FOREIGN KEY (owner_id, run_id, head_sha)
        REFERENCES runs (owner_id, id, head_sha),
    CONSTRAINT approvals_finding_fk FOREIGN KEY (owner_id, run_id, finding_id)
        REFERENCES findings (owner_id, run_id, id),
    CONSTRAINT approvals_finding_once UNIQUE (finding_id),
    CONSTRAINT approvals_owner_internal_unique UNIQUE (owner_id, id)
);

CREATE TABLE external_actions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    run_id bigint NOT NULL,
    action_type text NOT NULL,
    target_sha varchar(40) NOT NULL,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'proposed',
    remote_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT external_actions_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT external_actions_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT external_actions_type_present CHECK (length(btrim(action_type)) > 0),
    CONSTRAINT external_actions_target_sha_format CHECK (target_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT external_actions_idempotency_key_present CHECK (
        length(btrim(idempotency_key)) > 0
    ),
    CONSTRAINT external_actions_status_known CHECK (
        status IN ('proposed', 'publishing', 'published', 'failed')
    ),
    CONSTRAINT external_actions_run_fk FOREIGN KEY (owner_id, run_id, target_sha)
        REFERENCES runs (owner_id, id, head_sha),
    CONSTRAINT external_actions_target_unique UNIQUE (run_id, action_type, target_sha),
    CONSTRAINT external_actions_owner_idempotency_unique UNIQUE (owner_id, idempotency_key),
    CONSTRAINT external_actions_owner_internal_unique UNIQUE (owner_id, id)
);

CREATE TABLE run_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id varchar(26) NOT NULL UNIQUE,
    owner_id varchar(26) NOT NULL,
    run_id bigint NOT NULL,
    event_key text NOT NULL,
    event_type text NOT NULL,
    event_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_events_public_id_ulid CHECK (
        public_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT run_events_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT run_events_key_present CHECK (length(btrim(event_key)) > 0),
    CONSTRAINT run_events_type_present CHECK (length(btrim(event_type)) > 0),
    CONSTRAINT run_events_data_object CHECK (jsonb_typeof(event_data) = 'object'),
    CONSTRAINT run_events_run_fk FOREIGN KEY (owner_id, run_id)
        REFERENCES runs (owner_id, id),
    CONSTRAINT run_events_run_key_unique UNIQUE (run_id, event_key),
    CONSTRAINT run_events_owner_internal_unique UNIQUE (owner_id, id)
);

CREATE FUNCTION reject_run_event_mutation() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'run_events are append-only' USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER run_events_append_only
BEFORE UPDATE OR DELETE ON run_events
FOR EACH ROW EXECUTE FUNCTION reject_run_event_mutation();

CREATE TRIGGER run_events_reject_truncate
BEFORE TRUNCATE ON run_events
FOR EACH STATEMENT EXECUTE FUNCTION reject_run_event_mutation();

CREATE INDEX pull_requests_owner_updated_idx ON pull_requests (owner_id, updated_at DESC);
CREATE INDEX runs_owner_created_idx ON runs (owner_id, created_at DESC);
CREATE INDEX findings_owner_run_idx ON findings (owner_id, run_id);
CREATE INDEX approvals_owner_run_idx ON approvals (owner_id, run_id);
CREATE INDEX external_actions_owner_run_idx ON external_actions (owner_id, run_id);
CREATE INDEX run_events_owner_run_occurred_idx
    ON run_events (owner_id, run_id, occurred_at, id);
