"""Unit tests for fail-closed Docker sandbox orchestration."""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from pr_reliability_workers.sandbox import (
    DockerSandboxRunner,
    LocalDockerRuntime,
    RuntimeResult,
    SandboxCleanupError,
    SandboxLimits,
    SandboxRequest,
    SandboxUnavailableError,
)

IMAGE = f"sha256:{'a' * 64}"
ENGINE_CAPABILITIES = (
    b'{"os":"linux","memory":true,"swap":true,"cpu_period":true,"cpu_quota":true,"pids":true}\n'
)


@dataclass(frozen=True)
class RuntimeCall:
    arguments: tuple[str, ...]
    timeout_seconds: float
    output_limit_bytes: int


class ScriptedRuntime:
    def __init__(self, *results: RuntimeResult) -> None:
        self.results = deque(results)
        self.calls: list[RuntimeCall] = []
        self.source_snapshot: set[str] | None = None

    async def execute(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> RuntimeResult:
        self.calls.append(RuntimeCall(tuple(arguments), timeout_seconds, output_limit_bytes))
        if arguments[0] == "create":
            mount = _option(tuple(arguments), "--mount")
            source = Path(mount.split(",src=", 1)[1].split(",dst=", 1)[0])
            self.source_snapshot = {
                path.relative_to(source).as_posix() for path in source.rglob("*")
            }
        if not self.results:
            raise AssertionError(f"unexpected runtime call: {arguments}")
        return self.results.popleft()


class CancellationRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.remove_started = asyncio.Event()
        self.release_remove = asyncio.Event()
        self.info_calls = 0

    async def execute(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> RuntimeResult:
        del timeout_seconds, output_limit_bytes
        call = tuple(arguments)
        self.calls.append(call)
        if call[0] == "info":
            self.info_calls += 1
            if "MemoryLimit" in call[-1]:
                return runtime_result(stdout=ENGINE_CAPABILITIES)
            return runtime_result(stdout=b"linux\n")
        if call[0] == "create":
            return runtime_result(stdout=b"container-id\n")
        if call[:2] == ("start", "--attach"):
            return runtime_result(stdout=b"ok\n")
        if call[:2] == ("inspect", "--format"):
            return runtime_result(stdout=b"0\n")
        if call[0] == "rm":
            self.remove_started.set()
            await self.release_remove.wait()
            return runtime_result()
        if call[:3] == ("container", "ls", "--all"):
            return runtime_result()
        raise AssertionError(f"unexpected runtime call: {arguments}")


def runtime_result(
    *,
    return_code: int | None = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
    output_limit_exceeded: bool = False,
) -> RuntimeResult:
    return RuntimeResult(
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded,
    )


def test_runner_applies_every_isolation_limit_and_removes_container(tmp_path: Path) -> None:
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    (workspace / "input.txt").write_text("trusted fixture", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("secret", encoding="utf-8")
    runtime = ScriptedRuntime(
        runtime_result(stdout=ENGINE_CAPABILITIES),
        runtime_result(stdout=b"container-id\n"),
        runtime_result(stdout=b"ok\n", stderr=b"warning\n"),
        runtime_result(stdout=b"0\n"),
        runtime_result(),
        runtime_result(),
        runtime_result(stdout=b"linux\n"),
    )
    limits = SandboxLimits(
        timeout_seconds=12,
        cpu_count=0.5,
        memory_bytes=64 * 1024 * 1024,
        pids=32,
        workspace_bytes=8 * 1024 * 1024,
        temp_bytes=2 * 1024 * 1024,
        output_bytes=4096,
    )

    result = asyncio.run(
        DockerSandboxRunner(runtime).run(
            SandboxRequest(
                image=IMAGE,
                workspace=workspace,
                command=("python", "-c", "print('ok')"),
                limits=limits,
            )
        )
    )

    assert result.succeeded
    assert result.stdout == "ok\n"
    assert result.stderr == "warning\n"
    assert [call.arguments[0] for call in runtime.calls] == [
        "info",
        "create",
        "start",
        "inspect",
        "rm",
        "container",
        "info",
    ]
    capability_template = runtime.calls[0].arguments[-1]
    assert ".CPUCfsPeriod" in capability_template
    assert ".CPUCfsQuota" in capability_template
    create = runtime.calls[1].arguments
    assert _option(create, "--network") == "none"
    assert _option(create, "--log-driver") == "none"
    assert "--read-only" in create
    assert "--init" in create
    assert _option(create, "--cap-drop") == "ALL"
    assert _option(create, "--security-opt") == "no-new-privileges"
    assert _option(create, "--user") == "65534:65534"
    assert _option(create, "--cpus") == "0.5"
    assert _option(create, "--memory") == str(64 * 1024 * 1024) + "b"
    assert _option(create, "--memory-swap") == str(64 * 1024 * 1024) + "b"
    assert _option(create, "--pids-limit") == "32"
    assert _options(create, "--ulimit") == ["core=0:0", "nofile=1024:1024"]
    assert any(
        value.startswith("/workspace:rw,nosuid,nodev,size=8388608")
        for value in _options(create, "--tmpfs")
    )
    assert any(
        value.startswith("/tmp:rw,nosuid,nodev,noexec,size=2097152")
        for value in _options(create, "--tmpfs")
    )
    mount = _option(create, "--mount")
    assert _options(create, "--env") == ["HOME=/tmp", "TMPDIR=/tmp"]
    assert ",dst=/source,readonly" in mount
    assert "/var/run/docker.sock" not in mount
    copied_source = Path(mount.split(",src=", 1)[1].split(",dst=", 1)[0])
    assert not copied_source.exists()
    assert "sandbox-entry" in create
    assert create[-3:] == ("python", "-c", "print('ok')")
    assert runtime.calls[2].timeout_seconds == 12
    assert runtime.calls[2].output_limit_bytes == 4096
    assert runtime.calls[-3].arguments[:3] == ("rm", "--force", "--volumes")
    assert runtime.source_snapshot == {"input.txt"}
    assert not (workspace / "result.txt").exists()


def test_unavailable_engine_blocks_verification(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(runtime_result(return_code=1, stderr=b"daemon unavailable"))

    with pytest.raises(SandboxUnavailableError, match="Linux Docker engine"):
        asyncio.run(
            DockerSandboxRunner(runtime).run(
                SandboxRequest(image=IMAGE, workspace=tmp_path, command=("true",))
            )
        )

    assert len(runtime.calls) == 1


def test_engine_without_required_hard_limits_blocks_verification(tmp_path: Path) -> None:
    capabilities = ENGINE_CAPABILITIES.replace(b'"pids":true', b'"pids":false')
    runtime = ScriptedRuntime(runtime_result(stdout=capabilities))

    with pytest.raises(SandboxUnavailableError, match="hard limits"):
        asyncio.run(
            DockerSandboxRunner(runtime).run(
                SandboxRequest(image=IMAGE, workspace=tmp_path, command=("true",))
            )
        )

    assert len(runtime.calls) == 1


@pytest.mark.parametrize(
    ("limited_result", "expected_field"),
    [
        (runtime_result(return_code=None, stdout=b"partial", timed_out=True), "timed_out"),
        (
            runtime_result(
                return_code=None,
                stdout=b"bounded",
                output_limit_exceeded=True,
            ),
            "output_limit_exceeded",
        ),
    ],
)
def test_limit_termination_kills_and_removes_container(
    tmp_path: Path,
    limited_result: RuntimeResult,
    expected_field: str,
) -> None:
    runtime = ScriptedRuntime(
        runtime_result(stdout=ENGINE_CAPABILITIES),
        runtime_result(stdout=b"container-id\n"),
        limited_result,
        runtime_result(),
        runtime_result(),
        runtime_result(),
        runtime_result(stdout=b"linux\n"),
    )

    result = asyncio.run(
        DockerSandboxRunner(runtime).run(
            SandboxRequest(image=IMAGE, workspace=tmp_path, command=("slow",))
        )
    )

    assert not result.succeeded
    assert getattr(result, expected_field)
    assert result.exit_code is None
    assert [call.arguments[0] for call in runtime.calls] == [
        "info",
        "create",
        "start",
        "kill",
        "rm",
        "container",
        "info",
    ]


def test_cleanup_failure_blocks_success(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        runtime_result(stdout=ENGINE_CAPABILITIES),
        runtime_result(stdout=b"container-id\n"),
        runtime_result(stdout=b"ok\n"),
        runtime_result(stdout=b"0\n"),
        runtime_result(return_code=1, stderr=b"busy"),
    )

    with pytest.raises(SandboxCleanupError, match="verification is blocked"):
        asyncio.run(
            DockerSandboxRunner(runtime).run(
                SandboxRequest(image=IMAGE, workspace=tmp_path, command=("true",))
            )
        )


def test_residual_container_blocks_success(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        runtime_result(stdout=ENGINE_CAPABILITIES),
        runtime_result(stdout=b"container-id\n"),
        runtime_result(stdout=b"ok\n"),
        runtime_result(stdout=b"0\n"),
        runtime_result(),
        runtime_result(stdout=b"container-id\n"),
    )

    with pytest.raises(SandboxCleanupError, match="verification is blocked"):
        asyncio.run(
            DockerSandboxRunner(runtime).run(
                SandboxRequest(image=IMAGE, workspace=tmp_path, command=("true",))
            )
        )

    listing = runtime.calls[-1].arguments
    assert listing[:3] == ("container", "ls", "--all")
    assert listing[-1].startswith("name=^/pr-review-")


def test_cancellation_waits_for_removal_and_confirms_absence(tmp_path: Path) -> None:
    async def run() -> None:
        runtime = CancellationRuntime()
        task = asyncio.create_task(
            DockerSandboxRunner(runtime).run(
                SandboxRequest(image=IMAGE, workspace=tmp_path, command=("true",))
            )
        )
        await runtime.remove_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        runtime.release_remove.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [call[0] for call in runtime.calls][-3:] == ["rm", "container", "info"]

    asyncio.run(run())


def test_workspace_larger_than_tmpfs_is_rejected_before_container_create(
    tmp_path: Path,
) -> None:
    (tmp_path / "large.bin").write_bytes(b"x" * 1025)
    runtime = ScriptedRuntime(runtime_result(stdout=ENGINE_CAPABILITIES))

    with pytest.raises(ValueError, match="bounded staging"):
        asyncio.run(
            DockerSandboxRunner(runtime).run(
                SandboxRequest(
                    image=IMAGE,
                    workspace=tmp_path,
                    command=("true",),
                    limits=SandboxLimits(workspace_bytes=1024),
                )
            )
        )

    assert [call.arguments[0] for call in runtime.calls] == ["info"]


@pytest.mark.parametrize(
    "image",
    [
        "python:latest",
        "python:3.12",
        "example.invalid/image@sha256:not-a-digest",
        f"--privileged@sha256:{'a' * 64}",
    ],
)
def test_mutable_or_invalid_images_are_rejected(image: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="immutable sha256"):
        SandboxRequest(image=image, workspace=tmp_path, command=("true",))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("timeout_seconds", 901),
        ("staging_timeout_seconds", 121),
        ("cpu_count", 5),
        ("memory_bytes", 4 * 1024 * 1024 * 1024 + 1),
        ("pids", 513),
        ("pids", 1.5),
        ("workspace_bytes", 2 * 1024 * 1024 * 1024 + 1),
        ("workspace_entries", 200_001),
        ("temp_bytes", 512 * 1024 * 1024 + 1),
        ("output_bytes", 10 * 1024 * 1024 + 1),
    ],
)
def test_unsafe_limit_values_are_rejected(field_name: str, value: object) -> None:
    with pytest.raises(ValueError):
        SandboxLimits(**{field_name: value})


def test_local_runtime_enforces_output_and_time_bounds() -> None:
    runtime = LocalDockerRuntime(sys.executable)

    output_limited = asyncio.run(
        runtime.execute(
            ("-c", "print('x' * 100000)"),
            timeout_seconds=5,
            output_limit_bytes=1024,
        )
    )
    timed_out = asyncio.run(
        runtime.execute(
            ("-c", "import time; time.sleep(5)"),
            timeout_seconds=0.05,
            output_limit_bytes=1024,
        )
    )

    assert output_limited.output_limit_exceeded
    assert len(output_limited.stdout) + len(output_limited.stderr) == 1024
    assert timed_out.timed_out


def _option(arguments: tuple[str, ...], name: str) -> str:
    return arguments[arguments.index(name) + 1]


def _options(arguments: tuple[str, ...], name: str) -> list[str]:
    return [arguments[index + 1] for index, value in enumerate(arguments) if value == name]
