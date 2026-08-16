"""Thin activity boundary around provider and persistence operations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from opentelemetry import trace
from pr_reliability_observability import meter
from temporalio import activity

from ..workflows.types import ModelUsage, PublishRequest, StageRequest, StageResult, TerminalRequest
from .sandbox import SandboxVerificationOperation

StageOperation = Callable[[StageRequest], Awaitable[StageResult]]
PublishOperation = Callable[[PublishRequest], Awaitable[None]]
TerminalOperation = Callable[[TerminalRequest], Awaitable[None]]

_METER = meter()
_ACTIVITY_DURATION = _METER.create_histogram("pr.activity.duration", unit="s")
_RUN_DURATION = _METER.create_histogram("pr.run.duration", unit="s")
_RUN_USAGE = _METER.create_counter("pr.run.usage")
_ACTIVITY_RETRIES = _METER.create_counter("pr.activity.retries")


@dataclass(frozen=True)
class ActivityOperations:
    """Injected operations that must honor idempotency keys and activity cancellation.

    Database writes must also reject a terminal or mismatched run/head in the same transaction.
    """

    select_context: StageOperation
    analyze: StageOperation
    verify: SandboxVerificationOperation
    publish: PublishOperation
    record_terminal: TerminalOperation

    def __post_init__(self) -> None:
        if not isinstance(self.verify, SandboxVerificationOperation):
            raise TypeError("verify must use SandboxVerificationOperation")


class ReviewActivities:
    """Register stable activity names without provider details in workflow code."""

    def __init__(self, operations: ActivityOperations) -> None:
        self._operations = operations

    @activity.defn(name="select_context")
    async def select_context(self, request: StageRequest) -> StageResult:
        return await _run_observed(
            self._operations.select_context(request), request, "tool", "select_context"
        )

    @activity.defn(name="analyze")
    async def analyze(self, request: StageRequest) -> StageResult:
        return await _run_observed(self._operations.analyze(request), request, "model", "analyze")

    @activity.defn(name="verify")
    async def verify(self, request: StageRequest) -> StageResult:
        return await _run_observed(self._operations.verify(request), request, "tool", "verify")

    @activity.defn(name="publish")
    async def publish(self, request: PublishRequest) -> None:
        await _run_observed(self._operations.publish(request), request, "tool", "publish")

    @activity.defn(name="record_terminal")
    async def record_terminal(self, request: TerminalRequest) -> None:
        await _run_observed(
            self._operations.record_terminal(request), request, "persistence", "record_terminal"
        )
        span_attributes = {
            "pr.run.id": request.run_id,
            "pr.run.outcome": request.outcome.value,
        }
        metric_attributes = {"pr.run.outcome": request.outcome.value}
        if request.run_duration_ms is not None:
            _RUN_DURATION.record(request.run_duration_ms / 1_000, metric_attributes)
        usage_status = _usage_status(request.usage)
        usage_known = usage_status == "complete"
        cost_known = request.usage is not None and request.usage.cost_usd_micros is not None
        _RUN_USAGE.add(
            1,
            {
                **metric_attributes,
                "pr.usage.known": usage_known,
                "pr.usage.status": usage_status,
                "pr.cost.known": cost_known,
            },
        )
        span = trace.get_current_span()
        span.set_attributes(span_attributes)
        span.set_attribute("pr.usage.known", usage_known)
        span.set_attribute("pr.usage.status", usage_status)
        span.set_attribute("pr.cost.known", cost_known)
        if request.usage is not None:
            _set_usage_attributes(span, request.usage)
        if request.approval_wait_ms is not None:
            _record_wait_event(request)


async def _run_observed(operation, request, operation_kind: str, operation_name: str):
    started = time.perf_counter()
    info = activity.info()
    attributes = {
        "pr.run.id": request.run_id,
        "pr.head.sha": request.head_sha,
        "pr.operation.kind": operation_kind,
        "pr.operation.name": operation_name,
        "temporal.activity.attempt": info.attempt,
    }
    metric_attributes = {
        "pr.operation.kind": operation_kind,
        "pr.operation.name": operation_name,
    }
    current_span = trace.get_current_span()
    current_span.set_attributes(attributes)
    if info.attempt > 1:
        _ACTIVITY_RETRIES.add(1, metric_attributes)
    try:
        result = await _run_cancellable(operation)
        if isinstance(result, StageResult):
            usage_status = _usage_status(result.usage)
            current_span.set_attribute("pr.usage.known", usage_status == "complete")
            current_span.set_attribute("pr.usage.status", usage_status)
            current_span.set_attribute(
                "pr.cost.known",
                result.usage is not None and result.usage.cost_usd_micros is not None,
            )
            if result.usage is not None:
                _set_usage_attributes(current_span, result.usage)
        return result
    finally:
        _ACTIVITY_DURATION.record(time.perf_counter() - started, metric_attributes)


def _set_usage_attributes(span, usage) -> None:
    span.set_attribute(
        "pr.usage.tokens_known",
        usage.input_tokens is not None and usage.output_tokens is not None,
    )
    span.set_attribute("pr.cost.known", usage.cost_usd_micros is not None)
    if usage.input_tokens is not None:
        span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
    if usage.output_tokens is not None:
        span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
    if usage.cost_usd_micros is not None:
        span.set_attribute("pr.cost.usd_micros", usage.cost_usd_micros)


def _usage_status(usage: ModelUsage | None) -> str:
    if usage is None or (usage.input_tokens is None and usage.output_tokens is None):
        return "unknown"
    if usage.input_tokens is not None and usage.output_tokens is not None:
        return "complete"
    return "partial"


def _record_wait_event(request: TerminalRequest) -> None:
    trace.get_current_span().add_event(
        "approval.wait",
        attributes={
            "pr.run.id": request.run_id,
            "pr.wait.duration_ms": request.approval_wait_ms,
        },
    )


async def _run_cancellable[Result](operation: Awaitable[Result]) -> Result:
    """Heartbeat while an injected operation runs and cancel its task with the activity."""

    operation_task = asyncio.create_task(operation)
    try:
        while True:
            done, _ = await asyncio.wait((operation_task,), timeout=0.1)
            if done:
                return await operation_task
            activity.heartbeat()
    finally:
        if not operation_task.done():
            operation_task.cancel()
            with suppress(asyncio.CancelledError):
                await operation_task
