"""Thin activity boundary around provider and persistence operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from temporalio import activity

from ..workflows.types import PublishRequest, StageRequest, StageResult, TerminalRequest
from .sandbox import SandboxVerificationOperation

StageOperation = Callable[[StageRequest], Awaitable[StageResult]]
PublishOperation = Callable[[PublishRequest], Awaitable[None]]
TerminalOperation = Callable[[TerminalRequest], Awaitable[None]]


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
        return await _run_cancellable(self._operations.select_context(request))

    @activity.defn(name="analyze")
    async def analyze(self, request: StageRequest) -> StageResult:
        return await _run_cancellable(self._operations.analyze(request))

    @activity.defn(name="verify")
    async def verify(self, request: StageRequest) -> StageResult:
        return await _run_cancellable(self._operations.verify(request))

    @activity.defn(name="publish")
    async def publish(self, request: PublishRequest) -> None:
        await _run_cancellable(self._operations.publish(request))

    @activity.defn(name="record_terminal")
    async def record_terminal(self, request: TerminalRequest) -> None:
        await _run_cancellable(self._operations.record_terminal(request))


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
