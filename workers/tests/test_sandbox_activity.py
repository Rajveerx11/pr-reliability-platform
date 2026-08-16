"""Tests for mandatory sandbox verification activity wiring."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from pr_reliability_workers.activities import (
    ActivityOperations,
    GitHubComment,
    GitHubCommentPublishOperation,
    GitHubRestCommentClient,
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


class FakeGitHubClient:
    async def current_head_sha(self, repository: str, pull_request_number: int) -> str:
        del repository, pull_request_number
        return "a" * 40

    async def find_comment(
        self,
        repository: str,
        pull_request_number: int,
        marker: str,
        expected_body: str,
    ) -> GitHubComment | None:
        del repository, pull_request_number, marker, expected_body
        return None

    async def create_comment(
        self, repository: str, pull_request_number: int, body: str
    ) -> GitHubComment:
        del repository, pull_request_number, body
        return GitHubComment("1")


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

    async def unsafe_publish(request: PublishRequest) -> None:
        del request

    provider.create = lambda: replace(expected, publish=unsafe_publish)
    with pytest.raises(TypeError, match="GitHubCommentPublishOperation"):
        load_activity_operations(f"{provider.__name__}:create")

    provider.create = lambda: replace(  # type: ignore[attr-defined]
        expected,
        publish=GitHubCommentPublishOperation(
            lambda: None,  # type: ignore[arg-type,return-value]
            FakeGitHubClient(),
            lambda: "01J00000000000000000000001",
        ),
    )
    with pytest.raises(TypeError, match="GitHubRestCommentClient"):
        load_activity_operations(f"{provider.__name__}:create")

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

    async def terminal(request: TerminalRequest) -> None:
        del request

    return ActivityOperations(
        select_context=stage,
        analyze=stage,
        verify=SandboxVerificationOperation(prepare, runner, record),
        publish=GitHubCommentPublishOperation(
            lambda: None,  # type: ignore[arg-type,return-value]
            GitHubRestCommentClient("installation-token", 1),
            lambda: "01J00000000000000000000001",
        ),
        record_terminal=terminal,
    )
