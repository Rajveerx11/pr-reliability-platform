"""Temporal integration and replay tests for durable review orchestration."""

from __future__ import annotations

import asyncio
from collections import Counter

from pr_reliability_contracts import StartRunCommand
from pr_reliability_workers.activities import ActivityOperations, ReviewActivities
from pr_reliability_workers.dispatch import dispatch_start_run, workflow_id_for
from pr_reliability_workers.worker import (
    create_activity_worker,
    create_worker,
    create_workflow_worker,
)
from pr_reliability_workers.workflows import (
    ApprovalSignal,
    PullRequestReviewWorkflow,
    ReviewWorkflowInput,
    WorkflowOutcome,
)
from pr_reliability_workers.workflows.types import (
    PublishRequest,
    StageRequest,
    StageResult,
    TerminalRequest,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

OWNER_ID = "01J00000000000000000000001"
RUN_ID = "01J00000000000000000000002"
NEXT_RUN_ID = "01J00000000000000000000003"
LATEST_RUN_ID = "01J00000000000000000000006"
REPOSITORY_ID = "01J00000000000000000000004"
PULL_REQUEST_ID = "01J00000000000000000000005"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
NEXT_HEAD_SHA = "c" * 40
LATEST_HEAD_SHA = "d" * 40
TASK_QUEUE = "review-workflow-tests"


class RecordingOperations:
    def __init__(
        self,
        *,
        fail_analyze_once: bool = False,
        fail_publish: bool = False,
        block_analysis: bool = False,
        block_terminal: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.completed_keys: set[str] = set()
        self.fail_analyze_once = fail_analyze_once
        self.fail_publish = fail_publish
        self.block_analysis = block_analysis
        self.block_terminal = block_terminal
        self.analyze_attempts = 0
        self.analysis_started = asyncio.Event()
        self.release_analysis = asyncio.Event()
        self.terminal_started = asyncio.Event()
        self.release_terminal = asyncio.Event()

    async def select_context(self, request: StageRequest) -> StageResult:
        return self._complete("select_context", request, "context-ref")

    async def analyze(self, request: StageRequest) -> StageResult:
        self.calls.append(("analyze", request.idempotency_key))
        self.analyze_attempts += 1
        self.analysis_started.set()
        if self.block_analysis:
            await self.release_analysis.wait()
        if self.fail_analyze_once and self.analyze_attempts == 1:
            raise ApplicationError("retry analysis")
        self.completed_keys.add(request.idempotency_key)
        return StageResult("analysis-ref")

    async def verify(self, request: StageRequest) -> StageResult:
        return self._complete("verify", request, "verification-ref")

    async def publish(self, request: PublishRequest) -> None:
        self.calls.append(("publish", request.idempotency_key))
        if self.fail_publish:
            raise ApplicationError("publish failed")
        self.completed_keys.add(request.idempotency_key)

    async def record_terminal(self, request: TerminalRequest) -> None:
        self.calls.append(("record_terminal", request.idempotency_key))
        self.terminal_started.set()
        if self.block_terminal:
            await self.release_terminal.wait()
        self.completed_keys.add(request.idempotency_key)

    def _complete(self, name: str, request: StageRequest, output_ref: str) -> StageResult:
        self.calls.append((name, request.idempotency_key))
        self.completed_keys.add(request.idempotency_key)
        return StageResult(output_ref)

    def activities(self) -> ReviewActivities:
        return ReviewActivities(
            ActivityOperations(
                select_context=self.select_context,
                analyze=self.analyze,
                verify=self.verify,
                publish=self.publish,
                record_terminal=self.record_terminal,
            )
        )


def workflow_input(
    *,
    run_id: str = RUN_ID,
    head_sha: str = HEAD_SHA,
    generation: int = 1,
    approval_timeout_seconds: int = 3_600,
) -> ReviewWorkflowInput:
    return ReviewWorkflowInput(
        owner_id=OWNER_ID,
        run_id=run_id,
        generation=generation,
        repository_id=REPOSITORY_ID,
        pull_request_id=PULL_REQUEST_ID,
        pull_request_number=12,
        base_sha=BASE_SHA,
        head_sha=head_sha,
        token_budget=100_000,
        cost_budget_usd_micros=1_000_000,
        approval_timeout_seconds=approval_timeout_seconds,
        activity_timeout_seconds=30,
    )


def start_command(
    *, public_id: str, run_id: str, head_sha: str, generation: int = 1
) -> StartRunCommand:
    return StartRunCommand(
        schema_version="1",
        public_id=public_id,
        owner_id=OWNER_ID,
        run_id=run_id,
        generation=generation,
        repository_id=REPOSITORY_ID,
        pull_request_id=PULL_REQUEST_ID,
        pull_request_number=12,
        base_sha=BASE_SHA,
        head_sha=head_sha,
        token_budget=100_000,
        cost_budget_usd_micros=1_000_000,
    )


async def start_environment(operations: RecordingOperations):
    environment = await WorkflowEnvironment.start_time_skipping()
    worker = create_worker(environment.client, TASK_QUEUE, operations.activities())
    return environment, worker


async def wait_for_status(
    handle, state: str, *, head_sha: str = HEAD_SHA, run_id: str | None = None
):
    for _ in range(100):
        status = await handle.query(PullRequestReviewWorkflow.status)
        if (
            status.state == state
            and status.head_sha == head_sha
            and (run_id is None or status.run_id == run_id)
        ):
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(f"workflow did not reach {state} for {head_sha}")


def test_retry_uses_stable_keys_and_history_replays() -> None:
    async def run() -> None:
        operations = RecordingOperations(fail_analyze_once=True)
        environment, worker = await start_environment(operations)
        async with environment, worker:
            handle = await environment.client.start_workflow(
                PullRequestReviewWorkflow.run,
                workflow_input(),
                id="review-retry",
                task_queue=TASK_QUEUE,
            )
            await wait_for_status(handle, "awaiting_approval")
            await handle.signal(
                PullRequestReviewWorkflow.approve,
                ApprovalSignal(
                    run_id=RUN_ID,
                    head_sha=HEAD_SHA,
                    approved=True,
                    finding_ids=("01J00000000000000000000010",),
                    approval_ids=("01J00000000000000000000011",),
                    comment_body_ref="comment-ref",
                ),
            )
            result = await handle.result()
            history = await handle.fetch_history()

        assert result.outcome is WorkflowOutcome.PUBLISHED
        assert operations.analyze_attempts == 2
        analyze_keys = [key for name, key in operations.calls if name == "analyze"]
        assert len(analyze_keys) == 2
        assert len(set(analyze_keys)) == 1
        assert len(operations.completed_keys) == 5
        replay = await Replayer(workflows=[PullRequestReviewWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(run())


def test_split_production_workers_advance_through_all_registered_activities() -> None:
    async def run() -> None:
        environment = await WorkflowEnvironment.start_time_skipping()
        operations = RecordingOperations()
        workflow_worker = create_workflow_worker(environment.client, TASK_QUEUE)
        activity_worker = create_activity_worker(
            environment.client,
            TASK_QUEUE,
            operations.activities(),
        )
        async with environment, workflow_worker, activity_worker:
            handle = await environment.client.start_workflow(
                PullRequestReviewWorkflow.run,
                workflow_input(),
                id="production-workflow-worker",
                task_queue=TASK_QUEUE,
            )
            status = await wait_for_status(handle, "awaiting_approval")
            assert status.run_id == RUN_ID
            await handle.cancel()

    asyncio.run(run())


def test_cancel_and_timeout_are_explicit() -> None:
    async def run() -> None:
        operations = RecordingOperations()
        environment, worker = await start_environment(operations)
        async with environment, worker:
            cancelled = await environment.client.start_workflow(
                PullRequestReviewWorkflow.run,
                workflow_input(),
                id="review-cancelled",
                task_queue=TASK_QUEUE,
            )
            await cancelled.signal(PullRequestReviewWorkflow.cancel, "user cancelled")
            cancelled_result = await cancelled.result()

            timed_out = await environment.client.start_workflow(
                PullRequestReviewWorkflow.run,
                workflow_input(run_id=NEXT_RUN_ID, approval_timeout_seconds=1),
                id="review-timeout",
                task_queue=TASK_QUEUE,
            )
            timeout_result = await timed_out.result()

        assert cancelled_result.outcome is WorkflowOutcome.CANCELLED
        assert cancelled_result.reason == "user cancelled"
        assert timeout_result.outcome is WorkflowOutcome.TIMED_OUT
        assert timeout_result.reason == "approval timeout"

    asyncio.run(run())


def test_cancel_interrupts_obsolete_activity() -> None:
    async def run() -> None:
        operations = RecordingOperations(block_analysis=True)
        environment, worker = await start_environment(operations)
        async with environment, worker:
            handle = await environment.client.start_workflow(
                PullRequestReviewWorkflow.run,
                workflow_input(),
                id="review-interrupted",
                task_queue=TASK_QUEUE,
            )
            await operations.analysis_started.wait()
            await handle.signal(PullRequestReviewWorkflow.cancel, "new head arrived")
            result = await handle.result()

        assert result.outcome is WorkflowOutcome.CANCELLED
        assert not any(name == "verify" for name, _ in operations.calls)

    asyncio.run(run())


def test_approval_is_bound_to_current_run_and_consumed_after_verification() -> None:
    async def run() -> None:
        operations = RecordingOperations(block_analysis=True)
        environment, worker = await start_environment(operations)
        async with environment, worker:
            handle = await environment.client.start_workflow(
                PullRequestReviewWorkflow.run,
                workflow_input(),
                id="review-bound-approval",
                task_queue=TASK_QUEUE,
            )
            await operations.analysis_started.wait()
            await handle.signal(
                PullRequestReviewWorkflow.approve,
                ApprovalSignal(RUN_ID, HEAD_SHA, True, ("finding",), ("approval",), "comment"),
            )
            await handle.signal(
                PullRequestReviewWorkflow.approve,
                ApprovalSignal(
                    run_id=NEXT_RUN_ID,
                    head_sha=NEXT_HEAD_SHA,
                    approved=True,
                    finding_ids=("finding",),
                    approval_ids=("approval",),
                    comment_body_ref="comment",
                ),
            )
            operations.release_analysis.set()
            result = await handle.result()

        assert result.outcome is WorkflowOutcome.PUBLISHED
        assert [name for name, _ in operations.calls[:4]] == [
            "select_context",
            "analyze",
            "verify",
            "publish",
        ]

    asyncio.run(run())


def test_publish_failure_records_failed_outcome() -> None:
    async def run() -> None:
        operations = RecordingOperations(fail_publish=True)
        environment, worker = await start_environment(operations)
        async with environment, worker:
            handle = await environment.client.start_workflow(
                PullRequestReviewWorkflow.run,
                workflow_input(),
                id="review-publish-failure",
                task_queue=TASK_QUEUE,
            )
            await wait_for_status(handle, "awaiting_approval")
            await handle.signal(
                PullRequestReviewWorkflow.approve,
                ApprovalSignal(RUN_ID, HEAD_SHA, True, ("finding",), ("approval",), "comment"),
            )
            result = await handle.result()

        assert result.outcome is WorkflowOutcome.FAILED
        assert result.reason == "publish activity failed"
        assert any(
            "terminal:failed" in key for name, key in operations.calls if name == "record_terminal"
        )

    asyncio.run(run())


def test_new_head_continues_as_new_and_supersedes_old_run() -> None:
    async def run() -> None:
        operations = RecordingOperations()
        environment, worker = await start_environment(operations)
        async with environment, worker:
            first_command = start_command(
                public_id="01J00000000000000000000020",
                run_id=RUN_ID,
                head_sha=HEAD_SHA,
            )
            next_command = start_command(
                public_id="01J00000000000000000000021",
                run_id=NEXT_RUN_ID,
                head_sha=NEXT_HEAD_SHA,
                generation=2,
            )
            latest_command = start_command(
                public_id="01J00000000000000000000022",
                run_id=LATEST_RUN_ID,
                head_sha=LATEST_HEAD_SHA,
                generation=3,
            )
            handle = await dispatch_start_run(
                environment.client, first_command, task_queue=TASK_QUEUE
            )
            await wait_for_status(handle, "awaiting_approval")
            duplicate_handle = await dispatch_start_run(
                environment.client, first_command, task_queue=TASK_QUEUE
            )
            same_handle = await dispatch_start_run(
                environment.client, latest_command, task_queue=TASK_QUEUE
            )
            delayed_handle = await dispatch_start_run(
                environment.client, next_command, task_queue=TASK_QUEUE
            )
            assert handle.id == workflow_id_for(first_command)
            assert duplicate_handle.id == handle.id
            assert same_handle.id == handle.id
            assert delayed_handle.id == handle.id
            status = await wait_for_status(
                handle,
                "awaiting_approval",
                head_sha=LATEST_HEAD_SHA,
                run_id=LATEST_RUN_ID,
            )
            assert status.generation == 3

            await handle.signal(
                PullRequestReviewWorkflow.approve,
                ApprovalSignal(
                    run_id=LATEST_RUN_ID,
                    head_sha=LATEST_HEAD_SHA,
                    approved=True,
                    finding_ids=("01J00000000000000000000010",),
                    approval_ids=("01J00000000000000000000011",),
                    comment_body_ref="comment-ref",
                ),
            )
            result = await handle.result()

        assert result.run_id == LATEST_RUN_ID
        assert result.head_sha == LATEST_HEAD_SHA
        assert result.outcome is WorkflowOutcome.PUBLISHED
        terminal_keys = [key for name, key in operations.calls if name == "record_terminal"]
        assert f"{RUN_ID}:{HEAD_SHA}:terminal:cancelled" in terminal_keys
        counts = Counter(key for _, key in operations.calls)
        assert all(count == 1 for key, count in counts.items() if "analyze" not in key)

    asyncio.run(run())


def test_reopen_generation_supersedes_same_head_old_run() -> None:
    async def run() -> None:
        operations = RecordingOperations()
        environment, worker = await start_environment(operations)
        async with environment, worker:
            first_command = start_command(
                public_id="01J00000000000000000000030",
                run_id=RUN_ID,
                head_sha=HEAD_SHA,
            )
            reopened_command = start_command(
                public_id="01J00000000000000000000031",
                run_id=NEXT_RUN_ID,
                head_sha=HEAD_SHA,
                generation=2,
            )
            handle = await dispatch_start_run(
                environment.client, first_command, task_queue=TASK_QUEUE
            )
            await wait_for_status(handle, "awaiting_approval")
            await dispatch_start_run(environment.client, reopened_command, task_queue=TASK_QUEUE)
            status = await wait_for_status(handle, "awaiting_approval", run_id=NEXT_RUN_ID)
            assert status.run_id == NEXT_RUN_ID
            await handle.signal(
                PullRequestReviewWorkflow.approve,
                ApprovalSignal(NEXT_RUN_ID, HEAD_SHA, False),
            )
            result = await handle.result()

        assert result.run_id == NEXT_RUN_ID
        assert result.outcome is WorkflowOutcome.REJECTED
        terminal_keys = [key for name, key in operations.calls if name == "record_terminal"]
        assert f"{RUN_ID}:{HEAD_SHA}:terminal:cancelled" in terminal_keys

    asyncio.run(run())


def test_supersede_during_terminal_recording_starts_replacement() -> None:
    async def run() -> None:
        operations = RecordingOperations(block_terminal=True)
        environment, worker = await start_environment(operations)
        async with environment, worker:
            first_command = start_command(
                public_id="01J00000000000000000000040",
                run_id=RUN_ID,
                head_sha=HEAD_SHA,
            )
            replacement = start_command(
                public_id="01J00000000000000000000041",
                run_id=NEXT_RUN_ID,
                head_sha=NEXT_HEAD_SHA,
                generation=2,
            )
            handle = await dispatch_start_run(
                environment.client, first_command, task_queue=TASK_QUEUE
            )
            await wait_for_status(handle, "awaiting_approval")
            await handle.signal(
                PullRequestReviewWorkflow.approve,
                ApprovalSignal(RUN_ID, HEAD_SHA, False),
            )
            await operations.terminal_started.wait()
            await dispatch_start_run(environment.client, replacement, task_queue=TASK_QUEUE)
            operations.block_terminal = False
            operations.release_terminal.set()
            status = await wait_for_status(
                handle,
                "awaiting_approval",
                head_sha=NEXT_HEAD_SHA,
                run_id=NEXT_RUN_ID,
            )
            assert status.generation == 2
            await handle.signal(
                PullRequestReviewWorkflow.approve,
                ApprovalSignal(NEXT_RUN_ID, NEXT_HEAD_SHA, False),
            )
            result = await handle.result()

        assert result.run_id == NEXT_RUN_ID
        assert result.outcome is WorkflowOutcome.REJECTED

    asyncio.run(run())
