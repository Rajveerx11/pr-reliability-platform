"""Operations auth, relational lifecycle, heartbeat fencing and bounded metrics."""

import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pr_reliability_api.approvals import ApprovalInboxSettings
from pr_reliability_api.auth.sessions import Principal
from pr_reliability_api.db import apply_migrations
from pr_reliability_api.operations.routes import create_operations_router
from pr_reliability_api.operations.store import (
    OperationsStore,
    RunnerRegistration,
    RunnerSessionReplaced,
)

OWNER = "01J00000000000000000000001"
OTHER = "01J00000000000000000000002"
TOKEN = "test-operations-only"


def registration(owner=OWNER, workload="review", runner_id="review-1"):
    return RunnerRegistration(owner, runner_id, uuid4(), "pr-review", "0.1.0", workload, 4)


@pytest.fixture
def factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("CI must provide TEST_DATABASE_URL")
        pytest.skip("TEST_DATABASE_URL is required")
    schema = "test_" + uuid4().hex
    with psycopg.connect(url) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        connection.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        apply_migrations(connection)

    def create():
        connection = psycopg.connect(url)
        connection.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    try:
        yield create
    finally:
        with psycopg.connect(url) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def seed(factory, owner=OWNER, seq=10, state="queued"):
    public = lambda n: f"01J{n:023d}"
    with factory() as connection:
        repository = connection.execute(
            """INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
               VALUES (%s, %s, %s, 'owner/repo') RETURNING id""",
            (public(seq), owner, seq),
        ).fetchone()[0]
        pr = connection.execute(
            """INSERT INTO pull_requests (public_id, owner_id, repository_id, github_number,
                 base_sha, head_sha) VALUES (%s, %s, %s, 1, %s, %s) RETURNING id""",
            (public(seq + 1), owner, repository, "a" * 40, "b" * 40),
        ).fetchone()[0]
        run = connection.execute(
            """INSERT INTO runs (public_id, owner_id, pull_request_id, base_sha, head_sha,
                 token_budget, cost_budget_usd_micros, state, created_at)
               VALUES (%s, %s, %s, %s, %s, 100, 100, %s, now() - interval '10 seconds')
               RETURNING id""",
            (public(seq + 2), owner, pr, "a" * 40, "b" * 40, state),
        ).fetchone()[0]
    return (
        repository,
        run,
        SimpleNamespace(owner_id=owner, run_id=public(seq + 2), head_sha="b" * 40),
    )


def client_for(factory, sessions=None):
    app = FastAPI()
    app.include_router(
        create_operations_router(
            ApprovalInboxSettings(OWNER, OWNER, TOKEN), factory, sessions=sessions
        )
    )
    return TestClient(app)


def test_unauthorized_operations_fail_before_database():
    def forbidden():
        pytest.fail("unauthorized request reached database")

    client = client_for(forbidden)
    assert client.get("/api/operations/overview").status_code == 401
    assert client.get("/api/operations/metrics").status_code == 401
    assert client.post("/api/operations/runners/review-1/drain").status_code == 401


def test_non_admin_and_cross_owner_fail_before_database():
    class Sessions:
        def __init__(self, owner):
            self.owner = owner

        def authorize(self, request, *, admin=False):
            assert admin
            if self.owner == OWNER:
                raise HTTPException(403, "Administrator access required")
            return Principal(self.owner, self.owner, None, "other", "admin", [])

    for owner in (OWNER, OTHER):
        client = client_for(lambda: pytest.fail("wrong owner reached database"), Sessions(owner))
        assert client.get("/api/operations/overview").status_code == 403
        assert client.post("/api/operations/runners/review-1/drain").status_code == 403


def test_registration_rejects_unbounded_metadata():
    with pytest.raises(ValueError):
        RunnerRegistration(OWNER, "runner/path", uuid4(), "pr-review", "0.1.0", "review", 4)
    with pytest.raises(ValueError):
        RunnerRegistration(OWNER, "runner", uuid4(), "pr-review", "secret\n", "review", 4)
    with pytest.raises(ValueError):
        RunnerRegistration(OWNER, "runner", uuid4(), "pr-review", "0.1.0", "unknown", 4)


def test_lifecycle_waits_pass_rate_scope_and_terminal_facts(factory):
    store = OperationsStore(factory)
    runner = registration()
    store.register(runner)
    repository, run, request = seed(factory)
    seed(factory, OTHER, 20, "failed")
    snapshot = store.snapshot(OWNER, "pr-review")
    assert snapshot["queued"] == snapshot["queue_depth"] == 1
    assert snapshot["p50_wait_seconds"] is None
    assert snapshot["job_pass_rate"] is None
    with factory() as connection:
        connection.execute(
            """INSERT INTO run_events (public_id, owner_id, run_id, event_key, event_type,
                 event_data, occurred_at) VALUES (%s, %s, %s, 'dispatch',
                 'run.command_dispatched', '{"status":"accepted"}', now())""",
            ("01J00000000000000000000090", OWNER, run),
        )
    assert store.snapshot(OWNER, "pr-review")["assigned"] == 1
    store.observe_start(runner, request)
    store.observe_start(runner, request)  # retry cannot reset first start
    with factory() as connection:
        connection.execute("UPDATE runs SET state = 'verifying' WHERE id = %s", (run,))
        assert connection.execute("SELECT count(*) FROM operation_work").fetchone()[0] == 1
    snapshot = store.snapshot(OWNER, "pr-review", [repository])
    assert snapshot["running"] == snapshot["wait_samples"] == 1
    assert 9 <= snapshot["p50_wait_seconds"] <= 30
    assert snapshot["p50_wait_seconds"] == snapshot["p95_wait_seconds"]
    assert store.snapshot(OWNER, "pr-review", [])["running"] == 0
    with factory() as connection:
        connection.execute(
            """INSERT INTO run_events (public_id, owner_id, run_id, event_key, event_type,
                 event_data, occurred_at) VALUES (%s, %s, %s, 'verify',
                 'activity.verify.completed', '{"conclusion":"passed"}', now())""",
            ("01J00000000000000000000091", OWNER, run),
        )
        connection.execute("UPDATE runs SET state = 'published' WHERE id = %s", (run,))
    snapshot = store.snapshot(OWNER, "pr-review")
    assert snapshot["completed"] == snapshot["pass_rate_samples"] == 1
    assert snapshot["job_pass_rate"] == 1
    with factory() as connection:
        connection.execute("UPDATE runs SET state = 'cancelled' WHERE id = %s", (run,))
    assert store.snapshot(OWNER, "pr-review")["cancelled"] == 1
    assert store.snapshot(OWNER, "pr-review")["completed"] == 0


def test_stale_busy_drain_restart_fencing_and_work_preservation(factory):
    store = OperationsStore(factory)
    runner = registration()
    store.register(runner)
    _, run, request = seed(factory)
    store.observe_start(runner, request)
    assert store.heartbeat(runner, 2, "busy") is False
    snapshot = store.snapshot(OWNER, "pr-review")
    assert snapshot["active_capacity"] == 4
    assert snapshot["utilization"] == 0.5
    with factory() as connection:
        connection.execute(
            "UPDATE operation_runners SET heartbeat_at = now() - %s", (timedelta(seconds=46),)
        )
    assert store.snapshot(OWNER, "pr-review")["runners"][0]["state"] == "offline"
    assert store.snapshot(OWNER, "pr-review")["active_capacity"] == 0
    assert not store.request_drain(OTHER, runner.runner_id)
    assert store.request_drain(OWNER, runner.runner_id)
    assert store.heartbeat(runner, 2, "busy")
    assert store.snapshot(OWNER, "pr-review")["runners"][0]["state"] == "draining"
    assert store.snapshot(OWNER, "pr-review")["active_capacity"] == 0
    store.retire(runner)
    replacement = registration()
    store.register(replacement)
    with pytest.raises(RunnerSessionReplaced):
        store.heartbeat(runner, 0, "online")
    with pytest.raises(RunnerSessionReplaced):
        store.observe_start(runner, request)
    store.retire(runner)  # cannot retire the new process
    assert not store.heartbeat(replacement, 0, "online")
    assert store.snapshot(OWNER, "pr-review")["active_capacity"] == 4
    with factory() as connection:
        assert (
            connection.execute("SELECT state FROM runs WHERE id = %s", (run,)).fetchone()[0]
            == "queued"
        )
        assert connection.execute("SELECT count(*) FROM operation_work").fetchone()[0] == 1
    with pytest.raises(ValueError):
        store.observe_start(replacement, SimpleNamespace(owner_id=OTHER))


def test_admin_drain_and_metrics_are_bounded_private(factory):
    runner = registration()
    store = OperationsStore(factory)
    store.register(runner)
    seed(factory)
    client = client_for(factory)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    overview = client.get("/api/operations/overview", headers=headers)
    assert overview.status_code == 200
    assert overview.headers["cache-control"] == "no-store"
    assert overview.json()["temporal_history"] == "unavailable"
    metrics = client.get("/api/operations/metrics", headers=headers)
    assert metrics.status_code == 200
    assert '{queue="pr-review"}' in metrics.text
    assert "runner_id" not in metrics.text and "owner_id" not in metrics.text
    assert "pr_operations_job_pass_rate{" not in metrics.text  # unknown is not zero
    assert client.post("/api/operations/runners/review-1/drain", headers=headers).status_code == 202
    assert client.post("/api/operations/runners/missing/drain", headers=headers).status_code == 404
    assert (
        client.post("/api/operations/runners/path%20injection/drain", headers=headers).status_code
        == 422
    )


def test_percentiles_missing_history_and_recent_failure_window(factory):
    store = OperationsStore(factory)
    runner = registration()
    store.register(runner)
    for index, seconds in enumerate((10, 20, 90)):
        _, run, _request = seed(factory, seq=100 + index * 10, state="failed")
        # Fixture observations model three known first-activity waits on old completed jobs.
        with factory() as connection:
            connection.execute(
                "UPDATE runs SET created_at = now() - interval '2 days' WHERE id = %s", (run,)
            )
            connection.execute(
                """INSERT INTO operation_work (owner_id, run_id, queue, first_activity_at)
                   SELECT owner_id, id, 'pr-review', created_at + %s
                   FROM runs WHERE id = %s""",
                (timedelta(seconds=seconds), run),
            )
    seed(factory, seq=150, state="rejected")  # old run without a start observation
    snapshot = store.snapshot(OWNER, "pr-review")
    assert snapshot["wait_samples"] == 3
    assert snapshot["unknown_wait_runs"] == 1
    assert snapshot["p50_wait_seconds"] == 20
    assert snapshot["p95_wait_seconds"] == pytest.approx(83)
    assert snapshot["recent_failures"] == 3  # completion time, not creation time
    with factory() as connection:
        connection.execute(
            "UPDATE operation_work SET first_activity_at = first_activity_at - interval '3 days'"
        )
    snapshot = store.snapshot(OWNER, "pr-review")
    assert snapshot["wait_samples"] == 0
    assert snapshot["unknown_wait_runs"] == 4
    assert snapshot["p50_wait_seconds"] is None


def test_pass_rate_and_work_counts_use_the_same_queue_scope(factory):
    store = OperationsStore(factory)
    runner = registration()
    store.register(runner)
    for index, conclusion in enumerate(("passed", "failed")):
        _, run, request = seed(factory, seq=200 + index * 10)
        store.observe_start(runner, request)
        with factory() as connection:
            if index:
                connection.execute(
                    "UPDATE operation_work SET queue = 'other-queue' WHERE run_id = %s", (run,)
                )
            connection.execute(
                """INSERT INTO run_events (public_id, owner_id, run_id, event_key, event_type,
                     event_data, occurred_at) VALUES (%s, %s, %s, 'verify',
                     'activity.verify.completed', jsonb_build_object('conclusion', %s::text), now())""",
                (f"01J{300 + index:023d}", OWNER, run, conclusion),
            )
    snapshot = store.snapshot(OWNER, "pr-review")
    assert snapshot["queued"] == snapshot["pass_rate_samples"] == 1
    assert snapshot["job_pass_rate"] == 1
    snapshot = store.snapshot(OWNER, "other-queue")
    assert snapshot["queued"] == snapshot["pass_rate_samples"] == 1
    assert snapshot["job_pass_rate"] == 0


def test_unacknowledged_drain_survives_crashed_session_replacement(factory):
    store = OperationsStore(factory)
    old = registration()
    store.register(old)
    store.request_drain(OWNER, old.runner_id)
    replacement = registration()
    store.register(replacement)
    assert store.heartbeat(replacement, 0, "online")
    assert store.snapshot(OWNER, "pr-review")["runners"][0]["state"] == "draining"
    store.retire(old)  # a late old process cannot acknowledge the replacement's drain
    assert store.heartbeat(replacement, 0, "online")
    store.retire(replacement)
    assert not store.heartbeat(replacement, 0, "offline")


def test_public_shell_and_script_have_no_private_data_and_secure_headers():
    client = client_for(lambda: pytest.fail("static asset reached database"))
    page = client.get("/operations")
    assert page.status_code == 200
    assert "<title>Runner operations</title>" in page.text
    assert OWNER not in page.text and TOKEN not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    script = client.get("/operations/assets/operations.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.headers["cache-control"] == "no-store"
    assert "heartbeat" in script.text


def test_later_stage_pending_activity_wait_and_unknown_observations_are_owner_scoped(factory):
    from datetime import UTC, datetime

    from pr_reliability_workers.operation_alerts import evaluate_alerts

    store = OperationsStore(factory)
    repository, run, _request = seed(factory, state="selecting_context")
    seed(factory, OTHER, 20, state="analyzing")
    runner = registration(workload="workflow", runner_id="workflow-1")
    store.register(runner)
    with factory() as connection:
        pr = connection.execute(
            "SELECT pull_request_id FROM runs WHERE id = %s", (run,)
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO run_events (public_id, owner_id, run_id, event_key, event_type,
               event_data, occurred_at) VALUES (%s, %s, %s, 'dispatch',
               'run.command_dispatched', '{"status":"accepted"}', now())""",
            ("01J00000000000000000000090", OWNER, run),
        )
    assert store.queue_workflows(OWNER, "pr-review") == [(pr, "01J00000000000000000000011")]
    assert store.snapshot(OWNER, "pr-review")["queue_depth"] is None
    oldest = datetime.now(UTC) - timedelta(seconds=301)
    store.observe_pending(runner, [(pr, 1, oldest)])
    snapshot = store.snapshot(OWNER, "pr-review", [repository])
    assert snapshot["queued"] == 0 and snapshot["running"] == 1
    assert snapshot["queue_depth"] == 1 and snapshot["current_wait_seconds"] >= 301
    assert "stuck_queue" in evaluate_alerts(snapshot)
    assert store.snapshot(OWNER, "pr-review", [])["queue_depth"] == 0
    with factory() as connection:
        connection.execute(
            "UPDATE operation_pending_activities SET observed_at = now() - interval '46 seconds'"
        )
    snapshot = store.snapshot(OWNER, "pr-review")
    assert snapshot["queue_depth"] is None and snapshot["current_wait_seconds"] is None
    assert "queue_probe_unavailable" in evaluate_alerts(snapshot)
    store.observe_pending(runner, [(pr, 0, None)])
    assert store.snapshot(OWNER, "pr-review")["queue_depth"] == 0


@pytest.mark.parametrize(
    "state", ["selecting_context", "analyzing", "verifying", "awaiting_approval"]
)
def test_advanced_workflow_without_dispatch_receipt_is_observed(factory, state):
    from datetime import UTC, datetime

    store = OperationsStore(factory)
    _, run, _ = seed(factory, state=state)
    seed(factory, OTHER, 20, state=state)
    seed(factory, seq=30)  # An undispatched queued run must not be described.
    seed(factory, seq=40, state="published")
    _, other_queue_run, other_queue_request = seed(factory, seq=50, state=state)
    runner = registration(workload="workflow", runner_id="workflow-1")
    store.register(runner)
    store.observe_start(runner, other_queue_request)
    with factory() as connection:
        connection.execute(
            "UPDATE operation_work SET queue = 'other-queue' WHERE run_id = %s", (other_queue_run,)
        )
        pr = connection.execute(
            "SELECT pull_request_id FROM runs WHERE id = %s", (run,)
        ).fetchone()[0]
        assert connection.execute("SELECT count(*) FROM run_events").fetchone()[0] == 0
    # Temporal accepted and advanced the run before a crashed dispatcher saved its receipt.
    workflows = store.queue_workflows(OWNER, "pr-review")
    assert workflows == [(pr, "01J00000000000000000000011")]
    assert store.snapshot(OWNER, "pr-review")["queue_observation_unknown"] == 1
    oldest = datetime.now(UTC) - timedelta(seconds=301)
    store.observe_pending(runner, [(workflow[0], 1, oldest) for workflow in workflows])
    snapshot = store.snapshot(OWNER, "pr-review")
    assert snapshot["queue_observation_unknown"] == 0
    assert snapshot["queued"] == 1
    assert snapshot["queue_depth"] == 2  # Durable queued run plus the advanced pending activity.
    assert snapshot["current_wait_seconds"] >= 301
    assert snapshot["running"] == (state != "awaiting_approval")
    assert snapshot["awaiting_approval"] == (state == "awaiting_approval")
