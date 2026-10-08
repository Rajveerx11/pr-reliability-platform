"""Owner-scoped operational facts. Temporal remains the only scheduler."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class RunnerRegistration:
    owner_id: str
    runner_id: str
    session_id: UUID
    queue: str
    version: str
    workload: str
    capacity: int

    def __post_init__(self):
        if not _NAME.fullmatch(self.runner_id) or not _NAME.fullmatch(self.queue):
            raise ValueError("runner and queue must be bounded operator identifiers")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", self.version):
            raise ValueError("version must be a bounded build identifier")
        if self.workload not in {"workflow", "review"} or not 1 <= self.capacity <= 1000:
            raise ValueError("invalid workload or capacity")


class RunnerSessionReplaced(RuntimeError):
    """An older process must stop polling after its runner identity is replaced."""


class OperationsStore:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    def register(self, runner: RunnerRegistration) -> None:
        with self.connection_factory() as connection:
            connection.execute(
                """INSERT INTO operation_runners
                   (owner_id, runner_id, session_id, queue, version, workload, capacity, state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'online')
                   ON CONFLICT (owner_id, runner_id) DO UPDATE SET
                     session_id = EXCLUDED.session_id, queue = EXCLUDED.queue,
                     version = EXCLUDED.version, workload = EXCLUDED.workload,
                     capacity = EXCLUDED.capacity, active = 0, state = 'online',
                     heartbeat_at = now()
                   """,
                (
                    runner.owner_id,
                    runner.runner_id,
                    runner.session_id,
                    runner.queue,
                    runner.version,
                    runner.workload,
                    runner.capacity,
                ),
            )

    def heartbeat(self, runner: RunnerRegistration, active: int, state: str) -> bool:
        """Fence old process sessions. Return a durable operator drain request."""
        if not 0 <= active <= runner.capacity or state not in {
            "online",
            "busy",
            "draining",
            "offline",
        }:
            raise ValueError("invalid heartbeat")
        with self.connection_factory() as connection:
            row = connection.execute(
                """UPDATE operation_runners SET active = %s, state = %s, heartbeat_at = now()
                   WHERE owner_id = %s AND runner_id = %s AND session_id = %s
                   RETURNING drain_requested""",
                (active, state, runner.owner_id, runner.runner_id, runner.session_id),
            ).fetchone()
        if row is None:
            raise RunnerSessionReplaced("runner session replaced")
        return row[0]

    def observe_start(self, runner: RunnerRegistration, request) -> None:
        """An actual review activity start, not dispatch acceptance or a heartbeat."""
        if request.owner_id != runner.owner_id:
            raise ValueError("activity owner differs from runner owner")
        with self.connection_factory() as connection:
            if (
                connection.execute(
                    """SELECT 1 FROM operation_runners
                   WHERE owner_id = %s AND runner_id = %s AND session_id = %s FOR SHARE""",
                    (runner.owner_id, runner.runner_id, runner.session_id),
                ).fetchone()
                is None
            ):
                raise RunnerSessionReplaced("runner session replaced")
            connection.execute(
                """INSERT INTO operation_work (owner_id, run_id, queue)
                   SELECT owner_id, id, %s FROM runs
                   WHERE owner_id = %s AND public_id = %s AND head_sha = %s
                     AND state NOT IN ('published', 'rejected', 'failed', 'cancelled')
                   ON CONFLICT (owner_id, run_id) DO NOTHING""",
                (runner.queue, runner.owner_id, request.run_id, request.head_sha),
            )

    def retire(self, runner: RunnerRegistration) -> None:
        with self.connection_factory() as connection:
            connection.execute(
                """UPDATE operation_runners SET active = 0, state = 'offline',
                     drain_requested = false, heartbeat_at = now()
                   WHERE owner_id = %s AND runner_id = %s AND session_id = %s""",
                (runner.owner_id, runner.runner_id, runner.session_id),
            )

    def request_drain(self, owner_id: str, runner_id: str) -> bool:
        with self.connection_factory() as connection:
            return (
                connection.execute(
                    """UPDATE operation_runners SET drain_requested = true
                   WHERE owner_id = %s AND runner_id = %s RETURNING runner_id""",
                    (owner_id, runner_id),
                ).fetchone()
                is not None
            )

    def snapshot(self, owner_id: str, queue: str, repository_ids=None) -> dict:
        if not _NAME.fullmatch(queue):
            raise ValueError("invalid queue")
        with self.connection_factory() as connection:
            runners = connection.execute(
                """SELECT runner_id, version, workload, capacity, active,
                     CASE WHEN heartbeat_at < now() - interval '45 seconds' THEN 'offline'
                          WHEN drain_requested AND state != 'offline' THEN 'draining'
                          ELSE state END,
                     heartbeat_at
                   FROM operation_runners WHERE owner_id = %s AND queue = %s
                   ORDER BY runner_id LIMIT 1000""",
                (owner_id, queue),
            ).fetchall()
            row = connection.execute(
                """WITH facts AS (
                     SELECT r.id, r.state, r.created_at, r.updated_at, w.first_activity_at,
                       EXISTS (SELECT 1 FROM run_events e
                         WHERE e.owner_id = r.owner_id AND e.run_id = r.id
                           AND e.event_type = 'run.command_dispatched'
                           AND e.event_data->>'status' = 'accepted') AS assigned
                     FROM runs r JOIN pull_requests p
                       ON p.owner_id = r.owner_id AND p.id = r.pull_request_id
                     LEFT JOIN operation_work w ON w.owner_id = r.owner_id AND w.run_id = r.id
                     WHERE r.owner_id = %s
                       AND (%s::bigint[] IS NULL OR p.repository_id = ANY(%s))
                       AND (w.queue IS NULL OR w.queue = %s)
                   ) SELECT
                     count(*) FILTER (WHERE state = 'queued' AND NOT assigned),
                     count(*) FILTER (WHERE state = 'queued' AND assigned),
                     count(*) FILTER (WHERE state IN
                       ('selecting_context', 'analyzing', 'verifying')),
                     count(*) FILTER (WHERE state = 'awaiting_approval'),
                     count(*) FILTER (WHERE state = 'cancelled'),
                     count(*) FILTER (WHERE state IN ('published', 'rejected', 'failed')),
                     count(*) FILTER (WHERE state = 'queued'),
                     max(extract(epoch FROM (now() - created_at)))
                       FILTER (WHERE state = 'queued'),
                     percentile_cont(0.50) WITHIN GROUP
                       (ORDER BY extract(epoch FROM (first_activity_at - created_at)))
                       FILTER (WHERE first_activity_at >= created_at),
                     percentile_cont(0.95) WITHIN GROUP
                       (ORDER BY extract(epoch FROM (first_activity_at - created_at)))
                       FILTER (WHERE first_activity_at >= created_at),
                     count(*) FILTER (WHERE first_activity_at >= created_at),
                     count(*) FILTER (WHERE state != 'queued' AND
                       (first_activity_at IS NULL OR first_activity_at < created_at)),
                     count(*) FILTER (WHERE state = 'failed' AND updated_at > now() - interval '1 hour'),
                     count(*) FILTER (WHERE state IN ('published', 'rejected', 'failed')
                                       AND updated_at > now() - interval '1 hour')
                   FROM facts""",
                (owner_id, repository_ids, repository_ids, queue),
            ).fetchone()
            verification = connection.execute(
                """SELECT count(*) FILTER (WHERE event_data->>'conclusion' = 'passed'), count(*)
                   FROM run_events e JOIN runs r ON r.owner_id = e.owner_id AND r.id = e.run_id
                   JOIN pull_requests p ON p.owner_id = r.owner_id AND p.id = r.pull_request_id
                   WHERE e.owner_id = %s AND e.event_type = 'activity.verify.completed'
                     AND (%s::bigint[] IS NULL OR p.repository_id = ANY(%s))
                     AND e.event_data->>'conclusion' IN ('passed', 'failed')""",
                (owner_id, repository_ids, repository_ids),
            ).fetchone()
        keys = (
            "queued",
            "assigned",
            "running",
            "awaiting_approval",
            "cancelled",
            "completed",
            "queue_depth",
            "current_wait_seconds",
            "p50_wait_seconds",
            "p95_wait_seconds",
            "wait_samples",
            "unknown_wait_runs",
            "recent_failures",
            "recent_completed",
        )
        snapshot = dict(zip(keys, row, strict=True))
        snapshot["runners"] = [
            dict(
                zip(
                    (
                        "runner_id",
                        "version",
                        "workload",
                        "capacity",
                        "active",
                        "state",
                        "heartbeat_at",
                    ),
                    r,
                    strict=True,
                )
            )
            for r in runners
        ]
        # Workflow task occupancy is not observed by the activity interceptor.
        for runner in snapshot["runners"]:
            if runner["workload"] == "workflow":
                runner["active"] = runner["capacity"] = None
        available = [
            r
            for r in snapshot["runners"]
            if r["workload"] == "review" and r["state"] in {"online", "busy"}
        ]
        snapshot["active_workers"] = len(available)
        snapshot["active_capacity"] = sum(r["capacity"] for r in available)
        snapshot["active_slots"] = sum(r["active"] for r in available)
        capacity = snapshot["active_capacity"]
        snapshot["utilization"] = snapshot["active_slots"] / capacity if capacity else None
        snapshot["job_pass_rate"] = verification[0] / verification[1] if verification[1] else None
        snapshot["pass_rate_samples"] = verification[1]
        snapshot["schema_version"] = "1"
        snapshot["queue"] = queue
        snapshot["temporal_history"] = "unavailable"
        return snapshot
