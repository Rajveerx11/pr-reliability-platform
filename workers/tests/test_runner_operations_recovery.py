"""Temporal retries a blocked start across process replacement with real durable facts."""

import asyncio
import os
import threading
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from pr_reliability_api.db import apply_migrations
from pr_reliability_api.operations.store import OperationsStore, RunnerRegistration
from pr_reliability_workers.operations import RunnerMonitor, run_monitored
from pr_reliability_workers.workflows.types import StageRequest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

OWNER = "01J00000000000000000000001"
RUN = "01J00000000000000000000004"


@workflow.defn
class RecoveryProbeWorkflow:
    @workflow.run
    async def run(self, request: StageRequest) -> str:
        return await workflow.execute_activity(
            "recovery_probe",
            request,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=10),
        )


class RecoveryActivities:
    def __init__(self):
        self.calls = []

    @activity.defn(name="recovery_probe")
    async def work(self, request: StageRequest) -> str:
        self.calls.append(request.run_id)
        return request.run_id


@pytest.fixture
def durable_store():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("CI must provide TEST_DATABASE_URL")
        pytest.skip("TEST_DATABASE_URL is required")
    schema = "test_recovery_" + uuid4().hex
    unavailable = threading.Event()
    blocked = threading.Event()

    def connect():
        if unavailable.is_set():
            blocked.set()
            raise psycopg.OperationalError("test dependency outage")
        connection = psycopg.connect(url)
        connection.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    with psycopg.connect(url) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        connection.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        apply_migrations(connection)
        repository = connection.execute(
            """INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
               VALUES (%s, %s, 1, 'owner/repo') RETURNING id""",
            ("01J00000000000000000000002", OWNER),
        ).fetchone()[0]
        pr = connection.execute(
            """INSERT INTO pull_requests (public_id, owner_id, repository_id, github_number,
                 base_sha, head_sha) VALUES (%s, %s, %s, 1, %s, %s) RETURNING id""",
            ("01J00000000000000000000003", OWNER, repository, "a" * 40, "b" * 40),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO runs (public_id, owner_id, pull_request_id, base_sha, head_sha,
                 token_budget, cost_budget_usd_micros, state)
               VALUES (%s, %s, %s, %s, %s, 100, 100, 'queued')""",
            (RUN, OWNER, pr, "a" * 40, "b" * 40),
        )
    try:
        yield OperationsStore(connect), unavailable, blocked
    finally:
        unavailable.clear()
        with psycopg.connect(url) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_real_temporal_dependency_loss_and_restart_preserve_work(durable_store):
    async def scenario():
        store, unavailable, blocked = durable_store
        async with await WorkflowEnvironment.start_local() as environment:
            queue = "recovery-probe"
            operations = RecoveryActivities()

            def monitor():
                return RunnerMonitor(
                    store,
                    RunnerRegistration(OWNER, "review-1", uuid4(), queue, "0.1.0", "review", 1),
                )

            def worker(runner):
                return Worker(
                    environment.client,
                    task_queue=queue,
                    activities=[operations.work],
                    interceptors=[runner],
                    max_concurrent_activities=1,
                    graceful_shutdown_timeout=timedelta(seconds=5),
                )

            async with Worker(
                environment.client,
                task_queue=queue,
                workflows=[RecoveryProbeWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                first = monitor()
                store.register(first.registration)
                # Lose only this test's connection factory, never a user's database process.
                unavailable.set()
                async with worker(first):
                    handle = await environment.client.start_workflow(
                        RecoveryProbeWorkflow.run,
                        StageRequest(OWNER, RUN, "b" * 40, "recovery-test"),
                        id="recovery-test",
                        task_queue=queue,
                    )
                    assert await asyncio.to_thread(blocked.wait, 10)
                    assert operations.calls == []  # failure precedes provider side effects
                unavailable.clear()
                assert store.snapshot(OWNER, queue)["queued"] == 1
                assert store.snapshot(OWNER, queue)["wait_samples"] == 0
                replacement = monitor()
                polling = asyncio.create_task(
                    run_monitored(worker(replacement), environment.client, replacement)
                )
                try:
                    assert await asyncio.wait_for(handle.result(), 20) == RUN
                finally:
                    replacement.drain()
                    await asyncio.wait_for(polling, 10)
                snapshot = store.snapshot(OWNER, queue)
                assert operations.calls == [RUN]
                assert snapshot["wait_samples"] == 1
                assert snapshot["queued"] == 1  # monitor never invents a completion
                assert snapshot["runners"][0]["state"] == "offline"
                assert snapshot["active_capacity"] == 0

    asyncio.run(scenario())
