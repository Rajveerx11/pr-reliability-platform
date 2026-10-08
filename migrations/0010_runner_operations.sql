-- Operational observations supplement durable runs; they never schedule or cancel work.
CREATE TABLE operation_runners (
    owner_id varchar(26) NOT NULL,
    runner_id varchar(64) NOT NULL,
    session_id uuid NOT NULL,
    queue varchar(64) NOT NULL,
    version varchar(80) NOT NULL,
    workload text NOT NULL CHECK (workload IN ('workflow', 'review')),
    capacity integer NOT NULL CHECK (capacity BETWEEN 1 AND 1000),
    active integer NOT NULL DEFAULT 0 CHECK (active BETWEEN 0 AND 1000),
    state text NOT NULL CHECK (state IN ('online', 'busy', 'draining', 'offline')),
    drain_requested boolean NOT NULL DEFAULT false,
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, runner_id),
    CHECK (owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'),
    CHECK (runner_id ~ '^[a-zA-Z0-9_-]{1,64}$'),
    CHECK (queue ~ '^[a-zA-Z0-9_-]{1,64}$')
);

CREATE TABLE operation_work (
    owner_id varchar(26) NOT NULL,
    run_id bigint NOT NULL,
    queue varchar(64) NOT NULL,
    first_activity_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, run_id),
    FOREIGN KEY (owner_id, run_id) REFERENCES runs (owner_id, id),
    CHECK (queue ~ '^[a-zA-Z0-9_-]{1,64}$')
);

CREATE INDEX operation_runners_owner_queue_idx ON operation_runners (owner_id, queue);
-- No guessed start times for historical runs. Absence of an observation is Unknown.
