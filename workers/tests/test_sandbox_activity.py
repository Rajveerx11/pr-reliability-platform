"""Tests for mandatory sandbox verification activity wiring."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest
from pr_reliability_proof_adapter import ProofAdapter, ProofGateResult, ProofRequest
from pr_reliability_workers.activities import (
    ActivityOperations,
    SandboxRunner,
    SandboxVerificationOperation,
    VerificationEvidence,
)
from pr_reliability_workers.sandbox import (
    DockerSandboxRunner,
    RuntimeResult,
    SandboxRequest,
    SandboxResult,
)
from pr_reliability_workers.worker import load_activity_operations
from pr_reliability_workers.workflows.types import (
    PublishRequest,
    StageRequest,
    StageResult,
    TerminalRequest,
)
from temporalio.exceptions import ApplicationError

IMAGE = f"sha256:{'a' * 64}"
STAGE_REQUEST = StageRequest("owner", "run", "head", "key", base_sha="a" * 40)


class StaticRunner:
    def __init__(self, result: SandboxResult) -> None:
        self.result = result

    async def run(self, request: SandboxRequest) -> SandboxResult:
        del request
        return self.result


class StaticProofRunner:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    async def run(self, request: ProofRequest) -> ProofGateResult:
        del request
        return ProofGateResult("0.2.0", self.payload)


class ErrorProofRunner:
    async def run(self, request: ProofRequest) -> ProofGateResult:
        del request
        raise RuntimeError("gate crashed")


class FakeContainerRuntime:
    async def execute(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> RuntimeResult:
        del arguments, timeout_seconds, output_limit_bytes
        return RuntimeResult(0, b"", b"")


def test_failed_result_is_recorded_then_fails_without_retry(tmp_path: Path) -> None:
    recorded: list[VerificationEvidence] = []
    failed = SandboxResult(exit_code=137, stdout="", stderr="oom", duration_ms=5)

    async def prepare(request: StageRequest) -> SandboxRequest:
        del request
        return SandboxRequest(IMAGE, tmp_path, ("true",))

    async def record(request: StageRequest, result: VerificationEvidence) -> StageResult:
        del request
        recorded.append(result)
        return StageResult("evidence-ref")

    operation = SandboxVerificationOperation(
        prepare,
        StaticRunner(failed),
        record,
        _proof_adapter(passed=True),
    )
    with pytest.raises(ApplicationError) as raised:
        asyncio.run(operation(STAGE_REQUEST))

    assert recorded == [VerificationEvidence(sandbox=failed)]
    assert raised.value.type == "SandboxVerificationFailed"
    assert raised.value.non_retryable


def test_proof_rejection_is_recorded_then_blocks_output(tmp_path: Path) -> None:
    recorded: list[VerificationEvidence] = []
    passed = SandboxResult(exit_code=0, stdout="ok", stderr="", duration_ms=5)

    async def prepare(request: StageRequest) -> SandboxRequest:
        del request
        return SandboxRequest(IMAGE, tmp_path, ("true",))

    async def record(request: StageRequest, result: VerificationEvidence) -> StageResult:
        del request
        recorded.append(result)
        return StageResult("must-not-escape")

    operation = SandboxVerificationOperation(
        prepare,
        StaticRunner(passed),
        record,
        _proof_adapter(passed=False),
    )
    with pytest.raises(ApplicationError) as raised:
        asyncio.run(operation(STAGE_REQUEST))

    assert recorded[0].proof is not None
    assert not recorded[0].proof.passed
    assert raised.value.type == "ProofGateRejected"
    assert raised.value.non_retryable


def test_proof_error_is_recorded_then_blocks_output(tmp_path: Path) -> None:
    recorded: list[VerificationEvidence] = []
    passed = SandboxResult(exit_code=0, stdout="ok", stderr="", duration_ms=5)

    async def prepare(request: StageRequest) -> SandboxRequest:
        del request
        return SandboxRequest(IMAGE, tmp_path, ("true",))

    async def record(request: StageRequest, result: VerificationEvidence) -> StageResult:
        del request
        recorded.append(result)
        return StageResult("must-not-escape")

    operation = SandboxVerificationOperation(
        prepare,
        StaticRunner(passed),
        record,
        ProofAdapter(ErrorProofRunner()),
    )
    with pytest.raises(ApplicationError) as raised:
        asyncio.run(operation(STAGE_REQUEST))

    assert recorded == [VerificationEvidence(sandbox=passed, proof_error="proof gate failed")]
    assert raised.value.type == "ProofGateFailed"
    assert raised.value.non_retryable


def test_production_loader_requires_real_docker_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ModuleType("sandbox_test_provider")
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    provider.create = lambda: _operations(  # type: ignore[attr-defined]
        StaticRunner(SandboxResult(0, "", "", 1))
    )

    with pytest.raises(TypeError, match="DockerSandboxRunner"):
        load_activity_operations(f"{provider.__name__}:create")

    provider.create = lambda: _operations(  # type: ignore[attr-defined]
        DockerSandboxRunner(FakeContainerRuntime())
    )
    with pytest.raises(TypeError, match="DockerSandboxRunner"):
        load_activity_operations(f"{provider.__name__}:create")

    expected = _operations(DockerSandboxRunner())
    provider.create = lambda: expected  # type: ignore[attr-defined]
    assert load_activity_operations(f"{provider.__name__}:create") is expected


def _operations(runner: SandboxRunner) -> ActivityOperations:
    async def stage(request: StageRequest) -> StageResult:
        del request
        return StageResult("ref")

    async def prepare(request: StageRequest) -> SandboxRequest:
        del request
        return SandboxRequest(IMAGE, Path.cwd(), ("true",))

    async def record(request: StageRequest, result: VerificationEvidence) -> StageResult:
        del request, result
        return StageResult("verification-ref")

    async def publish(request: PublishRequest) -> None:
        del request

    async def terminal(request: TerminalRequest) -> None:
        del request

    return ActivityOperations(
        select_context=stage,
        analyze=stage,
        verify=SandboxVerificationOperation(
            prepare,
            runner,
            record,
            _proof_adapter(passed=True),
        ),
        publish=publish,
        record_terminal=terminal,
    )


def _proof_adapter(*, passed: bool) -> ProofAdapter:
    return ProofAdapter(
        StaticProofRunner(
            {
                "passed": passed,
                "reasons": ["ok" if passed else "blocked"],
                "findings": [] if passed else [{"rule": "blocked"}],
            }
        )
    )
