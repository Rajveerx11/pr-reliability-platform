"""Tests for mandatory sandbox verification activity wiring."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest
from pr_reliability_workers.activities import (
    ActivityOperations,
    SandboxRunner,
    SandboxVerificationOperation,
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
STAGE_REQUEST = StageRequest("owner", "run", "head", "key")


class StaticRunner:
    def __init__(self, result: SandboxResult) -> None:
        self.result = result

    async def run(self, request: SandboxRequest) -> SandboxResult:
        del request
        return self.result


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
    recorded: list[SandboxResult] = []
    failed = SandboxResult(exit_code=137, stdout="", stderr="oom", duration_ms=5)

    async def prepare(request: StageRequest) -> SandboxRequest:
        del request
        return SandboxRequest(IMAGE, tmp_path, ("true",))

    async def record(request: StageRequest, result: SandboxResult) -> StageResult:
        del request
        recorded.append(result)
        return StageResult("evidence-ref")

    operation = SandboxVerificationOperation(prepare, StaticRunner(failed), record)
    with pytest.raises(ApplicationError) as raised:
        asyncio.run(operation(STAGE_REQUEST))

    assert recorded == [failed]
    assert raised.value.type == "SandboxVerificationFailed"
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

    async def record(request: StageRequest, result: SandboxResult) -> StageResult:
        del request, result
        return StageResult("verification-ref")

    async def publish(request: PublishRequest) -> None:
        del request

    async def terminal(request: TerminalRequest) -> None:
        del request

    return ActivityOperations(
        select_context=stage,
        analyze=stage,
        verify=SandboxVerificationOperation(prepare, runner, record),
        publish=publish,
        record_terminal=terminal,
    )
