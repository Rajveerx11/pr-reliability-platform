ALTER TABLE runs DROP CONSTRAINT runs_commit_generation_unique;

WITH ordered_runs AS (
    SELECT id, row_number() OVER (
        PARTITION BY pull_request_id ORDER BY created_at, id
    ) AS generation
    FROM runs
)
UPDATE runs
SET generation = ordered_runs.generation
FROM ordered_runs
WHERE runs.id = ordered_runs.id;

DROP TRIGGER run_events_append_only ON run_events;

UPDATE run_events AS event
SET event_data = jsonb_set(event.event_data, '{generation}', to_jsonb(run.generation), true)
FROM runs AS run
WHERE event.run_id = run.id
  AND event.event_type = 'run.command_created';

CREATE TRIGGER run_events_append_only
BEFORE UPDATE OR DELETE ON run_events
FOR EACH ROW EXECUTE FUNCTION reject_run_event_mutation();

ALTER TABLE runs
    ADD CONSTRAINT runs_commit_generation_unique
        UNIQUE (pull_request_id, head_sha, generation),
    ADD CONSTRAINT runs_generation_unique UNIQUE (pull_request_id, generation);
