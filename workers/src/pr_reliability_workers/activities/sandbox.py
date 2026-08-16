"""Mandatory sandbox wrapper for production verification activities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pr_reliability_proof_adapter import (
    ProofAdapter,
    ProofGateError,
    ProofRequest,
    ProofVerdict,
)
from temporalio.exceptions import ApplicationError

from ..sandbox import SandboxRequest, SandboxResult
from ..workflows.types import StageRequest, StageResult


class SandboxRunner(Protocol):
    async def run(self, request: SandboxRequest) -> SandboxResult: ...


PrepareSandbox = Callable[[StageRequest], Awaitable[SandboxRequest]]


@dataclass(frozen=True)
class VerificationEvidence:
    """Bounded sandbox and Proof of Work evidence recorded before any output."""

    sandbox: SandboxResult
    proof: ProofVerdict | None = None
    proof_error: str | None = None


RecordVerificationEvidence = Callable[[StageRequest, VerificationEvidence], Awaitable[StageResult]]


@dataclass(frozen=True)
class SandboxVerificationOperation:
    """Require isolated tests and the local Proof of Work adapter to pass."""

    prepare: PrepareSandbox
    runner: SandboxRunner
    record: RecordVerificationEvidence
    proof: ProofAdapter

    async def __call__(self, request: StageRequest) -> StageResult:
        sandbox_request = await self.prepare(request)
        sandbox_result = await self.runner.run(sandbox_request)
        if not sandbox_result.succeeded:
            await self.record(request, VerificationEvidence(sandbox=sandbox_result))
            raise ApplicationError(
                "sandbox verification failed",
                type="SandboxVerificationFailed",
                non_retryable=True,
            )

        proof_request = ProofRequest(
            repository=sandbox_request.workspace,
            base_ref=request.base_sha or "HEAD",
            timeout_seconds=sandbox_request.limits.timeout_seconds,
        )
        try:
            verdict = await self.proof.verify(proof_request)
        except ProofGateError as exc:
            await self.record(
                request,
                VerificationEvidence(sandbox=sandbox_result, proof_error=str(exc)),
            )
            raise ApplicationError(
                "proof gate failed",
                type="ProofGateFailed",
                non_retryable=True,
            ) from exc

        stage_result = await self.record(
            request,
            VerificationEvidence(sandbox=sandbox_result, proof=verdict),
        )
        if not verdict.passed:
            raise ApplicationError(
                "proof gate rejected the changeset",
                type="ProofGateRejected",
                non_retryable=True,
            )
        return stage_result
