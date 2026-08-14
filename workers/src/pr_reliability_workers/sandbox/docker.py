"""Fail-closed Docker runner for untrusted pull request commands."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import (
    SandboxCleanupError,
    SandboxRequest,
    SandboxResult,
    SandboxRuntimeError,
    SandboxUnavailableError,
)

_CONTROL_TIMEOUT_SECONDS = 30.0
_CONTROL_OUTPUT_BYTES = 16 * 1024
_COPY_COMMAND = 'cp -R /source/. /workspace/ && exec "$@"'
_ENGINE_CAPABILITY_TEMPLATE = (
    '{"os":{{json .OSType}},"memory":{{json .MemoryLimit}},'
    '"swap":{{json .SwapLimit}},"cpu_period":{{json .CPUCfsPeriod}},'
    '"cpu_quota":{{json .CPUCfsQuota}},"pids":{{json .PidsLimit}}}'
)


@dataclass(frozen=True)
class RuntimeResult:
    """Bounded result from one local container-runtime command."""

    return_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limit_exceeded: bool = False


class ContainerRuntime(Protocol):
    """Small injectable boundary used by the Docker runner and unit tests."""

    async def execute(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> RuntimeResult: ...


class LocalDockerRuntime:
    """Execute Docker CLI arguments without a host shell."""

    def __init__(self, executable: str = "docker") -> None:
        self._executable = executable

    async def execute(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> RuntimeResult:
        executable = shutil.which(self._executable)
        if executable is None:
            raise SandboxUnavailableError("Docker CLI is unavailable")
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise SandboxUnavailableError("Docker CLI could not start") from error
        return await _capture_process(
            process,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )


class DockerSandboxRunner:
    """Copy a workspace into a constrained, disposable Linux container."""

    def __init__(self, runtime: ContainerRuntime | None = None) -> None:
        self._runtime = runtime or LocalDockerRuntime()

    @property
    def production_isolation_enabled(self) -> bool:
        """Return whether production uses the fixed Docker CLI boundary."""

        return type(self._runtime) is LocalDockerRuntime and self._runtime._executable == "docker"

    async def run(self, request: SandboxRequest) -> SandboxResult:
        workspace = request.workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("sandbox workspace must be a directory")

        await self._require_linux_engine()
        container_name = f"pr-review-{uuid.uuid4().hex}"
        started_at = time.monotonic()
        create_attempted = False
        result: SandboxResult | None = None

        try:
            with tempfile.TemporaryDirectory(prefix="pr-review-source-") as temporary:
                source = Path(temporary, "source")
                await _stage_workspace(
                    workspace,
                    source,
                    request.limits.workspace_bytes,
                    request.limits.workspace_entries,
                    request.limits.staging_timeout_seconds,
                )
                create_arguments = _create_arguments(request, source, container_name)
                create_attempted = True
                created = await self._runtime.execute(
                    create_arguments,
                    timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
                    output_limit_bytes=_CONTROL_OUTPUT_BYTES,
                )
                _require_control_success(created, "create sandbox container")

                attached = await self._runtime.execute(
                    ("start", "--attach", container_name),
                    timeout_seconds=request.limits.timeout_seconds,
                    output_limit_bytes=request.limits.output_bytes,
                )
                duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
                if attached.timed_out or attached.output_limit_exceeded:
                    await self._best_effort_kill(container_name)
                    result = SandboxResult(
                        exit_code=None,
                        stdout=_decode(attached.stdout),
                        stderr=_decode(attached.stderr),
                        duration_ms=duration_ms,
                        timed_out=attached.timed_out,
                        output_limit_exceeded=attached.output_limit_exceeded,
                    )
                elif attached.return_code is None:
                    raise SandboxRuntimeError("sandbox attach returned no process status")
                else:
                    inspected = await self._runtime.execute(
                        ("inspect", "--format", "{{.State.ExitCode}}", container_name),
                        timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
                        output_limit_bytes=_CONTROL_OUTPUT_BYTES,
                    )
                    _require_control_success(inspected, "read sandbox exit status")
                    try:
                        exit_code = int(inspected.stdout.strip())
                    except ValueError as error:
                        raise SandboxRuntimeError(
                            "sandbox returned an invalid exit status"
                        ) from error
                    result = SandboxResult(
                        exit_code=exit_code,
                        stdout=_decode(attached.stdout),
                        stderr=_decode(attached.stderr),
                        duration_ms=duration_ms,
                    )
        finally:
            if create_attempted:
                await self._remove_container(container_name)

        if result is None:
            raise SandboxRuntimeError("sandbox produced no result")
        return result

    async def _require_linux_engine(self) -> None:
        probe = await self._runtime.execute(
            ("info", "--format", _ENGINE_CAPABILITY_TEMPLATE),
            timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
            output_limit_bytes=_CONTROL_OUTPUT_BYTES,
        )
        if not _control_succeeded(probe):
            raise SandboxUnavailableError(
                "a reachable Linux Docker engine with hard limits is required for verification"
            )
        try:
            capabilities = json.loads(probe.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SandboxUnavailableError(
                "Docker engine returned invalid capability data"
            ) from error
        required = ("memory", "swap", "cpu_period", "cpu_quota", "pids")
        if capabilities.get("os") != "linux" or any(
            capabilities.get(name) is not True for name in required
        ):
            raise SandboxUnavailableError(
                "a reachable Linux Docker engine with hard limits is required for verification"
            )

    async def _best_effort_kill(self, container_name: str) -> None:
        await self._runtime.execute(
            ("kill", container_name),
            timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
            output_limit_bytes=_CONTROL_OUTPUT_BYTES,
        )

    async def _remove_container(self, container_name: str) -> None:
        cancellation: asyncio.CancelledError | None = None
        try:
            removed, cancellation = await _execute_cancellation_resistant(
                self._runtime,
                ("rm", "--force", "--volumes", container_name),
                cancellation,
            )
            if not _control_succeeded(removed):
                raise SandboxCleanupError(
                    "sandbox container cleanup failed; verification is blocked"
                )

            absent, cancellation = await _execute_cancellation_resistant(
                self._runtime,
                (
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"name=^/{container_name}$",
                ),
                cancellation,
            )
            if not _control_succeeded(absent) or absent.stdout.strip():
                raise SandboxCleanupError(
                    "sandbox container cleanup failed; verification is blocked"
                )

            engine, cancellation = await _execute_cancellation_resistant(
                self._runtime,
                ("info", "--format", "{{.OSType}}"),
                cancellation,
            )
            if not _control_succeeded(engine) or engine.stdout.strip() != b"linux":
                raise SandboxCleanupError(
                    "sandbox container cleanup failed; verification is blocked"
                )
        except SandboxCleanupError:
            raise
        except Exception as error:
            raise SandboxCleanupError(
                "sandbox container cleanup failed; verification is blocked"
            ) from error
        if cancellation is not None:
            raise cancellation


def _create_arguments(
    request: SandboxRequest,
    source: Path,
    container_name: str,
) -> tuple[str, ...]:
    limits = request.limits
    source_value = str(source.resolve())
    if "," in source_value:
        raise ValueError("sandbox source path must not contain a comma")
    return (
        "create",
        "--name",
        container_name,
        "--label",
        "pr-reliability.sandbox=true",
        "--log-driver",
        "none",
        "--network",
        "none",
        "--read-only",
        "--init",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65534:65534",
        "--cpus",
        str(limits.cpu_count),
        "--memory",
        f"{limits.memory_bytes}b",
        "--memory-swap",
        f"{limits.memory_bytes}b",
        "--pids-limit",
        str(limits.pids),
        "--ulimit",
        "core=0:0",
        "--ulimit",
        "nofile=1024:1024",
        "--tmpfs",
        f"/workspace:rw,nosuid,nodev,size={limits.workspace_bytes},mode=700,uid=65534,gid=65534",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,size={limits.temp_bytes},mode=1777",
        "--mount",
        f"type=bind,src={source_value},dst=/source,readonly",
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        request.image,
        "/bin/sh",
        "-c",
        _COPY_COMMAND,
        "sandbox-entry",
        *request.command,
    )


async def _stage_workspace(
    source: Path,
    destination: Path,
    byte_limit: int,
    entry_limit: int,
    timeout_seconds: float,
) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pr_reliability_workers.sandbox.stage",
        str(source),
        str(destination),
        str(byte_limit),
        str(entry_limit),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    staged = await _capture_process(
        process,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=_CONTROL_OUTPUT_BYTES,
    )
    if staged.timed_out:
        raise ValueError("sandbox workspace staging exceeded its time limit")
    if staged.output_limit_exceeded or staged.return_code != 0:
        raise ValueError("sandbox workspace failed bounded staging validation")


def _require_control_success(result: RuntimeResult, action: str) -> None:
    if not _control_succeeded(result):
        raise SandboxRuntimeError(f"Docker could not {action}")


def _control_succeeded(result: RuntimeResult) -> bool:
    return result.return_code == 0 and not result.timed_out and not result.output_limit_exceeded


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


async def _execute_cancellation_resistant(
    runtime: ContainerRuntime,
    arguments: Sequence[str],
    prior_cancellation: asyncio.CancelledError | None,
) -> tuple[RuntimeResult, asyncio.CancelledError | None]:
    operation = asyncio.create_task(
        runtime.execute(
            arguments,
            timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
            output_limit_bytes=_CONTROL_OUTPUT_BYTES,
        )
    )
    cancellation = prior_cancellation
    while True:
        try:
            return await asyncio.shield(operation), cancellation
        except asyncio.CancelledError as error:
            cancellation = error


async def _capture_process(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
) -> RuntimeResult:
    if process.stdout is None or process.stderr is None:
        raise SandboxRuntimeError("Docker output pipes are unavailable")
    remaining = output_limit_bytes
    limit_reached = asyncio.Event()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def read_stream(
        stream: asyncio.StreamReader,
        chunks: list[bytes],
    ) -> None:
        nonlocal remaining
        while remaining > 0:
            chunk = await stream.read(min(65_536, remaining))
            if not chunk:
                return
            # Recheck after the await because the other stream shares this budget.
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                remaining = 0
                limit_reached.set()
                return
            chunks.append(chunk)
            remaining -= len(chunk)

        # Exactly filling the inclusive budget is allowed. Probe one extra byte so
        # overflow is distinguished from EOF without storing evidence past the cap.
        if await stream.read(1):
            limit_reached.set()

    readers = (
        asyncio.create_task(read_stream(process.stdout, stdout_chunks)),
        asyncio.create_task(read_stream(process.stderr, stderr_chunks)),
    )
    process_wait = asyncio.create_task(process.wait())
    limit_wait = asyncio.create_task(limit_reached.wait())
    timed_out = False
    output_limit_exceeded = False
    try:
        done, _ = await asyncio.wait(
            (process_wait, limit_wait),
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            timed_out = True
        elif limit_wait in done and limit_reached.is_set() and not process_wait.done():
            output_limit_exceeded = True
        if timed_out or output_limit_exceeded:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        else:
            await process_wait
            await asyncio.gather(*readers)
            if limit_reached.is_set():
                output_limit_exceeded = True
    except asyncio.CancelledError:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        raise
    finally:
        limit_wait.cancel()
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(limit_wait, *readers, return_exceptions=True)

    return RuntimeResult(
        return_code=process.returncode,
        stdout=b"".join(stdout_chunks),
        stderr=b"".join(stderr_chunks),
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded,
    )
