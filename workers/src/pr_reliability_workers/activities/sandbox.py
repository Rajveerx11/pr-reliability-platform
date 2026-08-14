"""Mandatory sandbox wrapper for production verification activities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from temporalio.exceptions import ApplicationError

from ..sandbox import SandboxRequest, SandboxResult
from ..workflows.types import StageRequest, StageResult


class SandboxRunner(Protocol):
    async def run(self, request: SandboxRequest) -> SandboxResult: ...


PrepareSandbox = Callable[[StageRequest], Awaitable[SandboxRequest]]
RecordSandboxResult = Callable[[StageRequest, SandboxResult], Awaitable[StageResult]]


@dataclass(frozen=True)
class SandboxVerificationOperation:
    """Resolve, execute, and record verification without a host-execution path."""

    prepare: PrepareSandbox
    runner: SandboxRunner
    record: RecordSandboxResult

    async def __call__(self, request: StageRequest) -> StageResult:
        sandbox_request = await self.prepare(request)
        sandbox_result = await self.runner.run(sandbox_request)
        stage_result = await self.record(request, sandbox_result)
        if not sandbox_result.succeeded:
            raise ApplicationError(
                "sandbox verification failed",
                type="SandboxVerificationFailed",
                non_retryable=True,
            )
        return stage_result
