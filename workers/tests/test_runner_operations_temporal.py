"""Real Temporal drain: finish in-flight work and preserve queued work for restart."""

import asyncio
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import uuid4

from pr_reliability_api.operations.store import RunnerRegistration
from pr_reliability_workers.operations import RunnerMonitor, run_monitored
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker


@workflow.defn
class DrainProbeWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            "drain_probe", name, start_to_close_timeout=timedelta(seconds=30)
        )


class DrainActivities:
    def __init__(self):
        self.started = asyncio.Event()
        self.shutdown_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    @activity.defn(name="drain_probe")
    async def work(self, name: str) -> str:
        self.calls.append(name)
        if name == "first":
            self.started.set()
            await activity.wait_for_worker_shutdown()
            self.shutdown_seen.set()
            await self.release.wait()
        return name


def test_real_temporal_drain_preserves_queued_work_for_replacement_worker():
    async def scenario():
        async with await WorkflowEnvironment.start_local() as environment:
            queue = "drain-probe"
            activities = DrainActivities()
            store = MagicMock()
            store.heartbeat.return_value = False
            registration = RunnerRegistration(
                "01J00000000000000000000001", "probe", uuid4(), queue, "0.1.0", "review", 1
            )
            monitor = RunnerMonitor(store, registration)
            worker = Worker(
                environment.client,
                task_queue=queue,
                activities=[activities.work],
                interceptors=[monitor],
                max_concurrent_activities=1,
                graceful_shutdown_timeout=timedelta(seconds=10),
            )
            workflow_worker = Worker(
                environment.client,
                task_queue=queue,
                workflows=[DrainProbeWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            )
            async with workflow_worker:
                monitoring = asyncio.create_task(run_monitored(worker, environment.client, monitor))
                first = await environment.client.start_workflow(
                    DrainProbeWorkflow.run, "first", id="first", task_queue=queue
                )
                await asyncio.wait_for(activities.started.wait(), 10)
                second = await environment.client.start_workflow(
                    DrainProbeWorkflow.run, "second", id="second", task_queue=queue
                )
                monitor.drain()
                await asyncio.wait_for(activities.shutdown_seen.wait(), 10)
                store.retire.assert_not_called()
                activities.release.set()
                await asyncio.wait_for(monitoring, 10)
                assert await first.result() == "first"
                assert activities.calls == ["first"]
                store.retire.assert_called_once()
                async with Worker(
                    environment.client,
                    task_queue=queue,
                    activities=[activities.work],
                    max_concurrent_activities=1,
                ):
                    assert await asyncio.wait_for(second.result(), 10) == "second"
                assert activities.calls == ["first", "second"]

    asyncio.run(scenario())
