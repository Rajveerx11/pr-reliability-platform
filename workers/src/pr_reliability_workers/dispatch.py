"""Atomically start or supersede a pull request workflow."""

from __future__ import annotations

from datetime import timedelta

from pr_reliability_contracts import StartRunCommand
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from .workflows import PullRequestReviewWorkflow, ReviewWorkflowInput, SupersedeSignal


def workflow_id_for(command: StartRunCommand) -> str:
    """Keep one active workflow execution per owned pull request."""

    return f"pr-review:{command.owner_id}:{command.pull_request_id}"


async def dispatch_start_run(
    client: Client,
    command: StartRunCommand,
    *,
    task_queue: str,
) -> WorkflowHandle[ReviewWorkflowInput, object]:
    """Signal the active run or atomically start the first run for this pull request."""

    request = ReviewWorkflowInput(
        owner_id=command.owner_id,
        run_id=command.run_id,
        generation=command.generation,
        repository_id=command.repository_id,
        pull_request_id=command.pull_request_id,
        pull_request_number=command.pull_request_number,
        base_sha=command.base_sha,
        head_sha=command.head_sha,
        token_budget=command.token_budget,
        cost_budget_usd_micros=command.cost_budget_usd_micros,
    )
    return await client.start_workflow(
        PullRequestReviewWorkflow.run,
        request,
        id=workflow_id_for(command),
        task_queue=task_queue,
        execution_timeout=timedelta(days=30),
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="supersede",
        start_signal_args=[SupersedeSignal(request)],
        request_id=command.public_id,
    )
