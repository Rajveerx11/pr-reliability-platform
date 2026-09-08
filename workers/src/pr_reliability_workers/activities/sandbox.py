"""Mandatory sandbox wrapper for production verification activities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from pr_reliability_proof_adapter import (
    ProofAdapter,
    ProofGateError,
    ProofRequest,
    ProofVerdict,
)
from temporalio.exceptions import ApplicationError

from ..sandbox import (
    RepositoryCheckConfigError,
    SandboxCleanupError,
    SandboxError,
    SandboxRequest,
    SandboxResult,
    SandboxUnavailableError,
    VerificationPlan,
)
from ..workflows.types import StageRequest, StageResult


class SandboxRunner(Protocol):
    async def run(self, request: SandboxRequest) -> SandboxResult: ...


PrepareSandbox = Callable[[StageRequest], Awaitable[VerificationPlan]]


@dataclass(frozen=True)
class VerificationCheckEvidence:
    """Structured, bounded result for one repository-defined check."""

    name: str
    status: Literal["passed", "failed", "skipped"]
    reason_code: Literal["configuration_changed", "path_match", "no_matching_paths"]
    sandbox: SandboxResult | None = None
    error_code: (
        Literal["sandbox_unavailable", "sandbox_runtime", "sandbox_cleanup_failed"] | None
    ) = None


@dataclass(frozen=True)
class VerificationEvidence:
    """Bounded sandbox and Proof of Work evidence recorded before any output."""

    checks: tuple[VerificationCheckEvidence, ...] = ()
    proof: ProofVerdict | None = None
    proof_error: str | None = None
    config_error: str | None = None


RecordVerificationEvidence = Callable[[StageRequest, VerificationEvidence], Awaitable[StageResult]]


@dataclass(frozen=True)
class SandboxVerificationOperation:
    """Require isolated tests and the local Proof of Work adapter to pass."""

    prepare: PrepareSandbox
    runner: SandboxRunner
    record: RecordVerificationEvidence
    proof: ProofAdapter

    async def __call__(self, request: StageRequest) -> StageResult:
        try:
            plan = await self.prepare(request)
        except RepositoryCheckConfigError as exc:
            await self.record(request, VerificationEvidence(config_error=exc.code))
            raise ApplicationError(
                "repository check configuration failed validation",
                type="RepositoryCheckConfigurationInvalid",
                non_retryable=True,
            ) from exc

        if plan.replay_output_ref is not None:
            if plan.replay_passed:
                return StageResult(plan.replay_output_ref)
            raise ApplicationError(
                "verification previously failed",
                type="VerificationFailed",
                non_retryable=True,
            )

        check_evidence: list[VerificationCheckEvidence] = []
        for check in plan.checks:
            if check.request is None:
                check_evidence.append(
                    VerificationCheckEvidence(check.name, "skipped", check.reason_code)
                )
                continue
            try:
                sandbox_result = await self.runner.run(check.request)
            except SandboxError as exc:
                error_code, error_type = _sandbox_failure(exc)
                check_evidence.append(
                    VerificationCheckEvidence(
                        check.name,
                        "failed",
                        check.reason_code,
                        error_code=error_code,
                    )
                )
                await self.record(
                    request,
                    VerificationEvidence(checks=tuple(check_evidence)),
                )
                raise ApplicationError(
                    "sandbox infrastructure failed",
                    type=error_type,
                    non_retryable=True,
                ) from exc
            check_evidence.append(
                VerificationCheckEvidence(
                    check.name,
                    "passed" if sandbox_result.succeeded else "failed",
                    check.reason_code,
                    sandbox_result,
                )
            )

        checks = tuple(check_evidence)
        if any(check.status == "failed" for check in checks):
            await self.record(request, VerificationEvidence(checks=checks))
            raise ApplicationError(
                "sandbox verification failed",
                type="SandboxVerificationFailed",
                non_retryable=True,
            )

        proof_request = ProofRequest(
            repository=plan.workspace,
            head_sha=request.head_sha,
            base_ref=request.base_sha or "HEAD",
            timeout_seconds=plan.proof_timeout_seconds,
        )
        try:
            verdict = await self.proof.verify(proof_request)
        except ProofGateError as exc:
            await self.record(
                request,
                VerificationEvidence(checks=checks, proof_error=str(exc)),
            )
            raise ApplicationError(
                "proof gate failed",
                type="ProofGateFailed",
                non_retryable=True,
            ) from exc

        stage_result = await self.record(
            request,
            VerificationEvidence(checks=checks, proof=verdict),
        )
        if not verdict.passed:
            raise ApplicationError(
                "proof gate rejected the changeset",
                type="ProofGateRejected",
                non_retryable=True,
            )
        return stage_result


def _sandbox_failure(error: SandboxError) -> tuple[str, str]:
    if isinstance(error, SandboxCleanupError):
        return "sandbox_cleanup_failed", "SandboxCleanupFailed"
    if isinstance(error, SandboxUnavailableError):
        return "sandbox_unavailable", "SandboxUnavailable"
    return "sandbox_runtime", "SandboxRuntimeFailed"
