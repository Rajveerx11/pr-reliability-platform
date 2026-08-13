"""Durable pull request review orchestration."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.workflow import ActivityCancellationType

from .types import (
    ApprovalSignal,
    PublishRequest,
    ReviewWorkflowInput,
    ReviewWorkflowResult,
    ReviewWorkflowStatus,
    StageRequest,
    StageResult,
    SupersedeSignal,
    TerminalRequest,
    WorkflowOutcome,
)

_MAX_ACTIVITY_ATTEMPTS = 3


@workflow.defn
class PullRequestReviewWorkflow:
    """Run context, analysis, verification, approval, and publish in order."""

    def __init__(self) -> None:
        self._input: ReviewWorkflowInput | None = None
        self._state = "queued"
        self._approval: ApprovalSignal | None = None
        self._cancel_reason: str | None = None
        self._supersede: SupersedeSignal | None = None

    @workflow.run
    async def run(self, request: ReviewWorkflowInput) -> ReviewWorkflowResult:
        self._input = request
        if (
            self._supersede is not None
            and self._supersede.next_run.generation <= request.generation
        ):
            self._supersede = None
        if self._approval is not None and not self._approval_matches(self._approval):
            self._approval = None
        try:
            context = await self._stage("select_context", "selecting_context")
            if result := await self._honor_interrupt():
                return result
            if context is None:
                raise RuntimeError("activity stopped without an interrupt")
            analysis = await self._stage("analyze", "analyzing", context.output_ref)
            if result := await self._honor_interrupt():
                return result
            if analysis is None:
                raise RuntimeError("activity stopped without an interrupt")
            await self._stage("verify", "verifying", analysis.output_ref)
            if result := await self._honor_interrupt():
                return result
        except ActivityError:
            return await self._finish(WorkflowOutcome.FAILED, "review activity failed")
        self._state = "awaiting_approval"

        try:
            await workflow.wait_condition(
                lambda: any((self._approval, self._cancel_reason, self._supersede)),
                timeout=timedelta(seconds=request.approval_timeout_seconds),
                timeout_summary="human approval wait",
            )
        except TimeoutError:
            return await self._finish(
                WorkflowOutcome.TIMED_OUT,
                "approval timeout",
            )

        if result := await self._honor_interrupt():
            return result

        approval = self._approval
        if approval is None:
            raise RuntimeError("workflow woke without a terminal signal")
        if not approval.approved:
            return await self._finish(WorkflowOutcome.REJECTED, "human rejected findings")
        if (
            not approval.finding_ids
            or not approval.approval_ids
            or approval.comment_body_ref is None
        ):
            return await self._finish(WorkflowOutcome.REJECTED, "approval payload is incomplete")

        self._state = "publishing"
        try:
            await self._publish(approval)
        except ActivityError:
            return await self._finish(WorkflowOutcome.FAILED, "publish activity failed")
        return await self._finish(WorkflowOutcome.PUBLISHED, None)

    async def _stage(
        self, activity_name: str, state: str, input_ref: str | None = None
    ) -> StageResult | None:
        request = self._required_input()
        self._state = state
        activity_handle = workflow.start_activity(
            activity_name,
            StageRequest(
                owner_id=request.owner_id,
                run_id=request.run_id,
                head_sha=request.head_sha,
                idempotency_key=self._key(activity_name),
                input_ref=input_ref,
            ),
            result_type=StageResult,
            **self._activity_options(activity_name),
        )
        completed, result = await self._wait_for_activity_or_interrupt(activity_handle)
        return result if completed else None

    async def _publish(self, approval: ApprovalSignal) -> None:
        request = self._required_input()
        await workflow.execute_activity(
            "publish",
            PublishRequest(
                owner_id=request.owner_id,
                run_id=request.run_id,
                repository_id=request.repository_id,
                pull_request_number=request.pull_request_number,
                head_sha=request.head_sha,
                finding_ids=approval.finding_ids,
                approval_ids=approval.approval_ids,
                comment_body_ref=approval.comment_body_ref or "",
                idempotency_key=self._key("publish"),
            ),
            **self._activity_options("publish"),
        )

    async def _wait_for_activity_or_interrupt(self, activity_handle):
        interrupt = asyncio.create_task(
            workflow.wait_condition(lambda: bool(self._cancel_reason or self._supersede))
        )
        done, _ = await workflow.wait(
            (activity_handle, interrupt), return_when=asyncio.FIRST_COMPLETED
        )
        if interrupt in done:
            if not activity_handle.done():
                await workflow.sleep(timedelta(milliseconds=100))
            if not activity_handle.done():
                activity_handle.cancel()
            try:
                await activity_handle
            except (ActivityError, asyncio.CancelledError):
                pass
            return False, None
        interrupt.cancel()
        try:
            await interrupt
        except asyncio.CancelledError:
            pass
        return True, await activity_handle

    async def _honor_interrupt(self) -> ReviewWorkflowResult | None:
        if self._supersede is not None:
            await self._record_terminal(
                WorkflowOutcome.CANCELLED,
                f"superseded by {self._supersede.next_run.run_id}",
            )
            workflow.continue_as_new(self._supersede.next_run)
        if self._cancel_reason is not None:
            return await self._finish(WorkflowOutcome.CANCELLED, self._cancel_reason)
        return None

    async def _finish(self, outcome: WorkflowOutcome, reason: str | None) -> ReviewWorkflowResult:
        await self._record_terminal(outcome, reason)
        self._state = outcome.value
        request = self._required_input()
        if self._supersede is not None:
            workflow.continue_as_new(self._supersede.next_run)
        return ReviewWorkflowResult(request.run_id, request.head_sha, outcome, reason)

    async def _record_terminal(self, outcome: WorkflowOutcome, reason: str | None) -> None:
        request = self._required_input()
        await workflow.execute_activity(
            "record_terminal",
            TerminalRequest(
                owner_id=request.owner_id,
                run_id=request.run_id,
                head_sha=request.head_sha,
                outcome=outcome,
                reason=reason,
                idempotency_key=self._key(f"terminal:{outcome.value}"),
            ),
            **self._activity_options("record_terminal"),
        )

    def _activity_options(self, name: str) -> dict[str, object]:
        request = self._required_input()
        return {
            "activity_id": self._key(name),
            "start_to_close_timeout": timedelta(seconds=request.activity_timeout_seconds),
            "heartbeat_timeout": timedelta(seconds=1),
            "retry_policy": RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=10),
                maximum_attempts=_MAX_ACTIVITY_ATTEMPTS,
            ),
            "cancellation_type": ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        }

    def _key(self, step: str) -> str:
        request = self._required_input()
        return f"{request.run_id}:{request.head_sha}:{step}"

    def _required_input(self) -> ReviewWorkflowInput:
        if self._input is None:
            raise RuntimeError("workflow input is not initialized")
        return self._input

    def _approval_matches(self, approval: ApprovalSignal) -> bool:
        request = self._required_input()
        return approval.run_id == request.run_id and approval.head_sha == request.head_sha

    @workflow.signal
    def approve(self, approval: ApprovalSignal) -> None:
        if self._input is None or self._approval_matches(approval):
            self._approval = approval

    @workflow.signal
    def cancel(self, reason: str) -> None:
        self._cancel_reason = reason.strip() or "cancelled"

    @workflow.signal
    def supersede(self, signal: SupersedeSignal) -> None:
        current_generation = self._input.generation if self._input is not None else 0
        pending_generation = (
            self._supersede.next_run.generation if self._supersede is not None else 0
        )
        if signal.next_run.generation > max(current_generation, pending_generation):
            self._supersede = signal

    @workflow.query
    def status(self) -> ReviewWorkflowStatus:
        request = self._required_input()
        return ReviewWorkflowStatus(
            run_id=request.run_id,
            generation=request.generation,
            head_sha=request.head_sha,
            state=self._state,
            cancellation_reason=self._cancel_reason,
        )
