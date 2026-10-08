"""Authoritative later-stage and retry queues, not inferred durable run states."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from pr_reliability_api.operations.store import RunnerRegistration
from pr_reliability_workers.pending_activities import (
    observe_pending_activities,
    pending_summary,
)
from temporalio import activity, workflow
from temporalio.api.enums.v1 import PendingActivityState
from temporalio.api.workflow.v1 import PendingActivityInfo
from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
import test_runner_operations_recovery as recovery

OWNER = recovery.OWNER
durable_store = recovery.durable_store


def description(*items, queue="pr-review"):
    raw = DescribeWorkflowExecutionResponse(pending_activities=items)
    raw.execution_config.task_queue.name = queue
    return SimpleNamespace(raw_description=raw)


def test_pending_retry_backlog_and_unknown_times_are_not_false_zero():
    retry = PendingActivityInfo(
        state=PendingActivityState.PENDING_ACTIVITY_STATE_SCHEDULED, attempt=2
    )
    retry.scheduled_time.FromDatetime(datetime.now(UTC) - timedelta(seconds=301))
    started = PendingActivityInfo(state=PendingActivityState.PENDING_ACTIVITY_STATE_STARTED)
    depth, oldest = pending_summary(description(retry, started), "pr-review")
    assert depth == 1
    assert (datetime.now(UTC) - oldest).total_seconds() >= 301
    assert pending_summary(description(retry), "other-queue") == (None, None)
    retry.ClearField("scheduled_time")
    assert pending_summary(description(retry), "pr-review") == (None, None)


def test_observer_uses_only_owned_pr_workflows_and_persists_unknown_on_failure():
    async def scenario():
        runner = RunnerRegistration(
            "01J00000000000000000000001", "wf", uuid4(), "pr-review", "test", "workflow", 1
        )
        store = MagicMock()
        store.queue_workflows.return_value = [(7, "01J00000000000000000000003")]
        handle = MagicMock()
        handle.describe = AsyncMock(side_effect=TimeoutError())
        client = MagicMock()
        client.get_workflow_handle.return_value = handle
        await observe_pending_activities(client, store, runner)
        client.get_workflow_handle.assert_called_once_with(
            "pr-review:01J00000000000000000000001:01J00000000000000000000003"
        )
        store.observe_pending.assert_called_once_with(runner, [(7, None, None)])

    asyncio.run(scenario())


@workflow.defn
class LaterStageProbe:
    def __init__(self):
        self.proceed = False

    @workflow.signal
    def continue_analysis(self):
        self.proceed = True

    @workflow.run
    async def run(self):
        await workflow.execute_activity(
            "context_probe", start_to_close_timeout=timedelta(seconds=30)
        )
        await workflow.wait_condition(lambda: self.proceed)
        return await workflow.execute_activity(
            "analysis_probe",
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=2),
        )


@workflow.defn
class OccupySlotProbe:
    @workflow.run
    async def run(self):
        return await workflow.execute_activity(
            "verify_probe", start_to_close_timeout=timedelta(seconds=60)
        )


class LaterStageActivities:
    def __init__(self, store):
        self.store = store
        self.context_complete = asyncio.Event()
        self.verifying = asyncio.Event()
        self.release = asyncio.Event()
        self.retry_seen = asyncio.Event()

    @activity.defn(name="context_probe")
    async def context(self):
        with self.store.connection_factory() as connection:
            connection.execute("UPDATE runs SET state = 'selecting_context' WHERE owner_id = %s", (OWNER,))
        self.context_complete.set()

    @activity.defn(name="verify_probe")
    async def verify(self):
        self.verifying.set()
        await self.release.wait()

    @activity.defn(name="analysis_probe")
    async def analyze(self):
        if activity.info().attempt == 1:
            self.retry_seen.set()
            raise ApplicationError("fixed test retry")
        return "done"


def test_real_temporal_context_completed_then_analyze_queued_and_retry_backlog(durable_store):
    async def scenario():
        store, _, _ = durable_store
        runner = RunnerRegistration(OWNER, "workflow-1", uuid4(), "pending-probe", "test", "workflow", 1)
        store.register(runner)
        with store.connection_factory() as connection:
            run = connection.execute("SELECT id FROM runs WHERE owner_id = %s", (OWNER,)).fetchone()[0]
            connection.execute(
                """INSERT INTO run_events (public_id, owner_id, run_id, event_key, event_type,
                     event_data, occurred_at) VALUES (%s, %s, %s, 'dispatch',
                     'run.command_dispatched', '{"status":"accepted"}', now())""",
                ("01J00000000000000000000090", OWNER, run),
            )
        async with await WorkflowEnvironment.start_local() as environment:
            queue = runner.queue
            operations = LaterStageActivities(store)
            async with (
                Worker(
                    environment.client,
                    task_queue=queue,
                    workflows=[LaterStageProbe, OccupySlotProbe],
                    workflow_runner=UnsandboxedWorkflowRunner(),
                ),
                Worker(
                    environment.client,
                    task_queue=queue,
                    activities=[operations.context, operations.verify, operations.analyze],
                    max_concurrent_activities=1,
                ),
            ):
                handle = await environment.client.start_workflow(
                    LaterStageProbe.run, id=f"pr-review:{OWNER}:01J00000000000000000000003", task_queue=queue
                )
                await asyncio.wait_for(operations.context_complete.wait(), 10)
                blocker = await environment.client.start_workflow(
                    OccupySlotProbe.run, id="verify-slot", task_queue=queue
                )
                await asyncio.wait_for(operations.verifying.wait(), 10)
                await handle.signal(LaterStageProbe.continue_analysis)
                depth = 0
                for _ in range(100):
                    depth, oldest = pending_summary(await handle.describe(), queue)
                    if depth:
                        break
                    await asyncio.sleep(0.05)
                assert depth == 1 and oldest is not None  # context has already finished
                await observe_pending_activities(environment.client, store, runner)
                snapshot = store.snapshot(OWNER, queue)
                assert snapshot["queued"] == 0 and snapshot["running"] == 1
                assert snapshot["queue_depth"] == 1 and snapshot["current_wait_seconds"] is not None
                from pr_reliability_workers.operation_alerts import evaluate_alerts

                assert "stuck_queue" in evaluate_alerts(
                    {
                        "runners": [],
                        "queue_depth": depth,
                        "current_wait_seconds": 301,
                        "recent_failures": 0,
                        "recent_completed": 0,
                    }
                )
                operations.release.set()
                await blocker.result()
                await asyncio.wait_for(operations.retry_seen.wait(), 10)
                observed_retry = False
                for _ in range(100):
                    raw = (await handle.describe()).raw_description
                    if any(
                        item.attempt > 1
                        and item.state == PendingActivityState.PENDING_ACTIVITY_STATE_SCHEDULED
                        for item in raw.pending_activities
                    ):
                        assert pending_summary(SimpleNamespace(raw_description=raw), queue)[0] == 1
                        await observe_pending_activities(environment.client, store, runner)
                        assert store.snapshot(OWNER, queue)["queue_depth"] == 1
                        observed_retry = True
                        break
                    await asyncio.sleep(0.01)
                assert observed_retry
                assert await asyncio.wait_for(handle.result(), 20) == "done"

    asyncio.run(scenario())
