"""Read-only Temporal activity queue observations for the existing runner monitor."""

import asyncio
from datetime import UTC, timedelta

from temporalio.api.enums.v1 import PendingActivityState
from temporalio.service import RPCError


def pending_summary(description, queue: str):
    raw = description.raw_description
    if raw.execution_config.task_queue.name != queue:
        return None, None
    waiting = [
        item
        for item in raw.pending_activities
        if item.state
        not in {
            PendingActivityState.PENDING_ACTIVITY_STATE_STARTED,
            PendingActivityState.PENDING_ACTIVITY_STATE_CANCEL_REQUESTED,
        }
    ]
    # Scheduled time is the original enqueue time, also during server retry backoff.
    # Missing timestamps/state must not turn a nonempty queue into a zero wait.
    if any(not item.HasField("scheduled_time") for item in waiting):
        return None, None
    oldest = min((item.scheduled_time.ToDatetime(tzinfo=UTC) for item in waiting), default=None)
    return len(waiting), oldest


async def observe_pending_activities(client, store, runner):
    workflows = await asyncio.to_thread(store.queue_workflows, runner.owner_id, runner.queue)
    limit = asyncio.Semaphore(8)

    async def describe(row):
        pull_request_id, public_id = row
        async with limit:
            try:
                description = await client.get_workflow_handle(
                    f"pr-review:{runner.owner_id}:{public_id}"
                ).describe(rpc_timeout=timedelta(seconds=3))
                depth, oldest = pending_summary(description, runner.queue)
            except (RPCError, ConnectionError, TimeoutError):
                depth, oldest = None, None
            return pull_request_id, depth, oldest

    observations = await asyncio.gather(*(describe(row) for row in workflows))
    await asyncio.to_thread(store.observe_pending, runner, observations)
