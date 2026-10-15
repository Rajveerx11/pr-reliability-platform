-- Migration: persist structured review performance metrics.
-- Issue: #39 — feat: persist complete review metrics and analytics.
--
-- Adds a run_metrics table keyed to each review run. All nullable columns
-- stay NULL when the fact is unavailable; they are never stored as zero or
-- estimated. Metrics are written once at run completion and are immutable.

CREATE TABLE run_metrics (
    id             bigint  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id       varchar(26) NOT NULL,
    run_id         bigint  NOT NULL,
    -- Final run outcome ('published', 'rejected', 'failed', 'cancelled').
    outcome        text    NOT NULL CHECK (
                       outcome IN ('published', 'rejected', 'failed', 'cancelled')
                   ),
    -- Per-stage duration in milliseconds. NULL when the stage did not occur.
    queue_wait_ms          bigint CHECK (queue_wait_ms >= 0),
    context_ms             bigint CHECK (context_ms >= 0),
    model_ms               bigint CHECK (model_ms >= 0),
    verification_ms        bigint CHECK (verification_ms >= 0),
    approval_wait_ms       bigint CHECK (approval_wait_ms >= 0),
    publish_ms             bigint CHECK (publish_ms >= 0),
    total_ms               bigint CHECK (total_ms >= 0),
    -- Attempt and retry counters. NULL when unknown.
    activity_attempts      integer CHECK (activity_attempts >= 0),
    activity_retries       integer CHECK (activity_retries >= 0),
    activity_timeouts      integer CHECK (activity_timeouts >= 0),
    -- Token and cost facts. NULL when unknown; never stored as zero to mean
    -- "no usage reported".
    input_tokens           bigint  CHECK (input_tokens >= 0),
    output_tokens          bigint  CHECK (output_tokens >= 0),
    total_tokens           bigint  CHECK (total_tokens >= 0),
    -- Exact provider-reported cost in USD micro-units. NULL when unknown.
    cost_usd_micros        bigint  CHECK (cost_usd_micros >= 0),
    -- 'full', 'partial', or 'unknown' — copied from ModelUsage.coverage.
    usage_coverage         text    CHECK (usage_coverage IN ('full', 'partial', 'unknown')),
    recorded_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_metrics_owner_id_ulid CHECK (
        owner_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    ),
    CONSTRAINT run_metrics_run_fk FOREIGN KEY (owner_id, run_id)
        REFERENCES runs (owner_id, id),
    -- One metrics row per run; re-running must update, not insert.
    CONSTRAINT run_metrics_run_unique UNIQUE (run_id)
);

CREATE INDEX run_metrics_owner_outcome_idx ON run_metrics (owner_id, outcome);
CREATE INDEX run_metrics_owner_recorded_idx ON run_metrics (owner_id, recorded_at DESC);
