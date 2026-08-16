"""Translate Proof of Work results into a small platform-owned verdict."""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

PROOF_VERDICT_VERSION = 1
_MAX_TIMEOUT_SECONDS = 900
_MAX_GATE_OUTPUT_BYTES = 256 * 1024
_MAX_GATE_ERROR_BYTES = 64 * 1024
_MAX_VERDICT_ITEMS = 128
_MAX_VERDICT_TEXT_LENGTH = 1_000
_PROCESS_CLEANUP_SECONDS = 5
_GIT_CHECK_SECONDS = 5
_SNAPSHOT_SECONDS = 120


@dataclass(frozen=True)
class ProofRequest:
    """Inputs needed to inspect one reviewed repository without running its tests."""

    repository: Path
    head_sha: str
    base_ref: str = "HEAD"
    timeout_seconds: float = 60

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path):
            raise TypeError("proof repository must be a Path")
        if not _is_git_sha(self.head_sha):
            raise ValueError("proof head_sha must be a lowercase 40-character Git SHA")
        if not isinstance(self.base_ref, str) or (
            self.base_ref != "HEAD"
            and (
                len(self.base_ref) != 40
                or any(character not in "0123456789abcdef" for character in self.base_ref)
            )
        ):
            raise ValueError("proof base_ref must be HEAD or a lowercase 40-character Git SHA")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("proof timeout_seconds must be finite and positive")


@dataclass(frozen=True)
class ProofGateResult:
    """Untrusted result at the published-package boundary."""

    package_version: str
    payload: object


@dataclass(frozen=True)
class ProofVerdict:
    """Small versioned verdict consumed by platform application code."""

    version: int
    passed: bool
    reasons: tuple[str, ...]
    finding_rules: tuple[str, ...]
    package_version: str


class ProofGateRunner(Protocol):
    async def run(self, request: ProofRequest) -> ProofGateResult: ...


class ProofGateError(RuntimeError):
    """Proof gate did not produce a trustworthy verdict."""


class ProofGateTimeoutError(ProofGateError):
    """Proof gate exceeded its platform-owned time limit."""


class ProofGateExecutionError(ProofGateError):
    """Proof gate failed or returned an invalid result."""


class PublishedProofGate:
    """Run the published-package boundary in a killable isolated process."""

    def __init__(self, worker_command: tuple[str, ...] | None = None) -> None:
        default_command = (
            sys.executable,
            "-m",
            "pr_reliability_proof_adapter._published_gate",
        )
        self._worker_command = worker_command or default_command
        self._production_command = worker_command is None
        if not self._worker_command or any(not item for item in self._worker_command):
            raise ValueError("proof worker command must contain non-empty arguments")

    @property
    def production_command_enabled(self) -> bool:
        """Return true only for the fixed published-package entry point."""
        return self._production_command

    async def run(self, request: ProofRequest) -> ProofGateResult:
        try:
            repository = request.repository.resolve(strict=True)
        except OSError as exc:
            raise ProofGateExecutionError("proof repository is not a Git checkout") from exc
        if not repository.is_dir() or not (repository / ".git").exists():
            raise ProofGateExecutionError("proof repository is not a Git checkout")

        with tempfile.TemporaryDirectory(prefix="pr-proof-gate-") as temporary:
            trusted_directory = Path(temporary).resolve(strict=True)
            if trusted_directory.is_symlink() or trusted_directory.is_relative_to(repository):
                raise ProofGateExecutionError("proof log directory is not isolated")
            snapshot = trusted_directory / "repository"
            log_path = trusted_directory / "log.db"
            status_path = trusted_directory / "worker.status"
            ready_path = trusted_directory / "worker.ready"
            if any(
                path.exists() or path.is_symlink()
                for path in (snapshot, log_path, status_path, ready_path)
            ):
                raise ProofGateExecutionError("proof log path is not isolated")
            await _await_repository_snapshot(
                _create_repository_snapshot,
                repository,
                snapshot,
                request.base_ref,
                request.head_sha,
            )

            worker = await _start_worker(
                self._worker_command,
                snapshot,
                request.base_ref,
                log_path,
                status_path,
                ready_path,
            )
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    stdout, stderr, returncode = await _collect_bounded_output(worker)
            except TimeoutError as exc:
                raise ProofGateTimeoutError("proof gate timed out") from exc
            except asyncio.CancelledError:
                raise
            finally:
                await _ensure_process_tree_terminated(worker)

            if returncode != 0:
                del stderr
                raise ProofGateExecutionError("proof gate failed")
            try:
                envelope = json.loads(stdout)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ProofGateExecutionError("proof gate returned a malformed result") from exc
            if not isinstance(envelope, Mapping) or envelope.get("ok") is not True:
                raise ProofGateExecutionError("proof gate failed")
            return ProofGateResult(
                package_version=envelope.get("package_version"),
                payload=envelope.get("payload"),
            )


class ProofAdapter:
    """Apply timeout and shape checks before returning a platform verdict."""

    def __init__(self, runner: ProofGateRunner | None = None) -> None:
        self._runner = runner or PublishedProofGate()

    @property
    def production_gate_enabled(self) -> bool:
        """Return true only for the platform-owned published-package boundary."""
        return type(self._runner) is PublishedProofGate and self._runner.production_command_enabled

    async def verify(self, request: ProofRequest) -> ProofVerdict:
        try:
            if type(self._runner) is PublishedProofGate:
                result = await self._runner.run(request)
            else:
                result = await asyncio.wait_for(
                    self._runner.run(request), timeout=request.timeout_seconds
                )
        except TimeoutError as exc:
            raise ProofGateTimeoutError("proof gate timed out") from exc
        except ProofGateError:
            raise
        except Exception as exc:
            raise ProofGateExecutionError("proof gate failed") from exc
        try:
            return _translate(result)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProofGateExecutionError("proof gate returned a malformed result") from exc


def _translate(result: ProofGateResult) -> ProofVerdict:
    if not isinstance(result, ProofGateResult):
        raise TypeError("result must be ProofGateResult")
    if not isinstance(result.package_version, str) or not result.package_version.strip():
        raise ValueError("package_version must be a non-empty string")
    if not isinstance(result.payload, Mapping):
        raise TypeError("payload must be a mapping")

    passed = result.payload["passed"]
    reasons = result.payload["reasons"]
    findings = result.payload["findings"]
    if type(passed) is not bool:
        raise TypeError("passed must be a boolean")
    if (
        not isinstance(reasons, list)
        or len(reasons) > _MAX_VERDICT_ITEMS
        or any(
            not isinstance(item, str) or len(item) > _MAX_VERDICT_TEXT_LENGTH for item in reasons
        )
    ):
        raise TypeError("reasons must be a list of strings")
    if not isinstance(findings, list) or len(findings) > _MAX_VERDICT_ITEMS:
        raise TypeError("findings must be a list")

    finding_rules: list[str] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            raise TypeError("each finding must be a mapping")
        rule = finding["rule"]
        if not isinstance(rule, str) or not rule or len(rule) > _MAX_VERDICT_TEXT_LENGTH:
            raise ValueError("each finding rule must be a non-empty string")
        if rule not in finding_rules:
            finding_rules.append(rule)

    return ProofVerdict(
        version=PROOF_VERDICT_VERSION,
        passed=passed,
        reasons=tuple(reasons),
        finding_rules=tuple(finding_rules),
        package_version=result.package_version,
    )


async def _start_worker(
    worker_command: tuple[str, ...],
    repository: Path,
    base_ref: str,
    log_path: Path,
    status_path: Path,
    ready_path: Path,
) -> _ManagedWorker:
    options: dict[str, object] = {}
    if os.name == "posix":
        options["start_new_session"] = True
    elif os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    creation = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pr_reliability_proof_adapter._process_supervisor",
            str(status_path),
            str(ready_path),
            *worker_command,
            str(repository),
            base_ref,
            str(log_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **options,
        )
    )
    try:
        process = await asyncio.shield(creation)
    except asyncio.CancelledError:
        try:
            process = await creation
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=_PROCESS_CLEANUP_SECONDS)
        except BaseException as cleanup_exc:
            raise ProofGateExecutionError("proof gate process cleanup failed") from cleanup_exc
        raise
    except OSError as exc:
        raise ProofGateExecutionError("proof gate failed") from exc
    job: object | None = None
    try:
        if os.name == "nt":
            from ._windows_job import WindowsJob

            job = WindowsJob.attach(process.pid)
        await asyncio.wait_for(
            _wait_for_process_file(process, ready_path, "lifecycle setup"),
            timeout=_PROCESS_CLEANUP_SECONDS,
        )
        if process.stdin is None:
            raise RuntimeError("proof process supervisor has no control pipe")
        process.stdin.write(b"1")
        await process.stdin.drain()
        return _ManagedWorker(process, job, status_path)
    except BaseException as exc:
        try:
            if os.name == "nt" and job is None:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=_PROCESS_CLEANUP_SECONDS)
            else:
                await _ensure_process_tree_terminated(_ManagedWorker(process, job, status_path))
        except BaseException as cleanup_exc:
            raise ProofGateExecutionError("proof gate process cleanup failed") from cleanup_exc
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ProofGateExecutionError("proof gate lifecycle setup failed") from exc


async def _collect_bounded_output(
    worker: _ManagedWorker,
) -> tuple[bytes, bytes, int]:
    process = worker.process
    if process.stdout is None or process.stderr is None:
        raise ProofGateExecutionError("proof gate failed")
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, _MAX_GATE_OUTPUT_BYTES))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, _MAX_GATE_ERROR_BYTES))
    status_task = asyncio.create_task(_wait_for_worker_status(worker))
    tasks = (stdout_task, stderr_task, status_task)
    try:
        pending: set[asyncio.Task[object]] = set(tasks)
        while not status_task.done():
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if exception := task.exception():
                    raise exception
        await _ensure_process_tree_terminated(worker)
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        return stdout, stderr, status_task.result()
    except ValueError as exc:
        raise ProofGateExecutionError("proof gate output exceeded its limit") from exc
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await reader.read(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise ValueError("proof gate output limit exceeded")
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass
class _ManagedWorker:
    process: asyncio.subprocess.Process
    job: object | None
    status_path: Path
    cleanup_attempted: bool = False


async def _terminate_process_tree(worker: _ManagedWorker) -> None:
    if worker.cleanup_attempted:
        return
    worker.cleanup_attempted = True
    process = worker.process
    try:
        if os.name == "posix":
            if not sys.platform.startswith("linux"):
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                raise RuntimeError("proof descendant cleanup requires Linux")
            await asyncio.to_thread(_kill_linux_process_tree, process.pid)
        elif os.name == "nt":
            if worker.job is None:
                raise RuntimeError("proof process has no Windows Job Object")
            worker.job.terminate()
        elif process.returncode is None:
            process.kill()
        if process.stdin is not None:
            process.stdin.close()
            await asyncio.wait_for(process.stdin.wait_closed(), timeout=_PROCESS_CLEANUP_SECONDS)
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_CLEANUP_SECONDS)
    except (Exception, TimeoutError) as exc:
        raise ProofGateExecutionError("proof gate process cleanup failed") from exc


def _kill_linux_process_tree(root_pid: int) -> None:
    with suppress(ProcessLookupError):
        os.kill(root_pid, signal.SIGSTOP)
    deadline = time.monotonic() + _PROCESS_CLEANUP_SECONDS
    while True:
        descendants = _linux_descendants(root_pid)
        living = [pid for pid in descendants if _linux_process_state(pid) != "Z"]
        if not living:
            break
        for process_id in reversed(living):
            with suppress(ProcessLookupError):
                os.kill(process_id, signal.SIGKILL)
        if time.monotonic() >= deadline:
            raise TimeoutError("proof descendants did not terminate")
        time.sleep(0.01)
    with suppress(ProcessLookupError):
        os.kill(root_pid, signal.SIGKILL)


def _linux_descendants(root_pid: int) -> list[int]:
    found: list[int] = []
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        task_directory = Path(f"/proc/{parent}/task")
        try:
            task_paths = tuple(task_directory.iterdir())
        except OSError:
            continue
        children: set[int] = set()
        for task_path in task_paths:
            try:
                raw_children = (task_path / "children").read_text(encoding="ascii")
            except OSError:
                continue
            children.update(int(value) for value in raw_children.split())
        found.extend(children)
        pending.extend(children)
    return found


def _linux_process_state(process_id: int) -> str | None:
    try:
        stat = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
    except OSError:
        return None
    _, separator, remainder = stat.rpartition(") ")
    return remainder[:1] if separator else None


async def _ensure_process_tree_terminated(worker: _ManagedWorker) -> None:
    cleanup = asyncio.create_task(_terminate_process_tree(worker))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


async def _wait_for_worker_status(worker: _ManagedWorker) -> int:
    await _wait_for_process_file(worker.process, worker.status_path, "worker status")
    try:
        value = int(await asyncio.to_thread(worker.status_path.read_text, encoding="ascii"))
    except (OSError, ValueError) as exc:
        raise ProofGateExecutionError("proof gate returned invalid process status") from exc
    if not -255 <= value <= 255:
        raise ProofGateExecutionError("proof gate returned invalid process status")
    return value


async def _wait_for_process_file(
    process: asyncio.subprocess.Process,
    path: Path,
    description: str,
) -> None:
    while not path.exists():
        if process.returncode is not None:
            raise ProofGateExecutionError(f"proof {description} failed")
        await asyncio.sleep(0.01)


def _create_repository_snapshot(
    repository: Path,
    snapshot: Path,
    base_ref: str,
    expected_head: str,
) -> None:
    _validate_checkout(repository, base_ref, expected_head, require_clean=True)
    environment = os.environ.copy()
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    _run_git_command(
        (
            "git",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--quiet",
            "--no-local",
            "--no-checkout",
            repository.as_uri(),
            str(snapshot),
        ),
        cwd=repository.parent,
        timeout=_SNAPSHOT_SECONDS,
        environment=environment,
    )
    _run_git_command(
        ("git", "checkout", "--quiet", "--detach", expected_head),
        cwd=snapshot,
        timeout=_SNAPSHOT_SECONDS,
        environment=environment,
    )
    _validate_checkout(snapshot, base_ref, expected_head, require_clean=True)


async def _await_repository_snapshot(function, *arguments) -> None:
    snapshot = asyncio.create_task(asyncio.to_thread(function, *arguments))
    try:
        await asyncio.shield(snapshot)
    except asyncio.CancelledError:
        await snapshot
        raise


def _validate_checkout(
    repository: Path,
    base_ref: str,
    expected_head: str,
    *,
    require_clean: bool = False,
) -> None:
    head = _git(repository, "rev-parse", "--verify", "HEAD^{commit}")
    if head.stdout.strip() != expected_head:
        raise ProofGateExecutionError("proof repository head does not match request")
    if require_clean:
        status = _git(repository, "status", "--porcelain=v1", "--untracked-files=all")
        if status.stdout:
            raise ProofGateExecutionError("proof repository must be clean")
    base = _git(repository, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    try:
        ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor", base.stdout.strip(), expected_head),
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_GIT_CHECK_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProofGateExecutionError("proof gate failed") from exc
    if ancestor.returncode != 0:
        raise ProofGateExecutionError("proof base is not an ancestor of head")


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _run_git_command(
        ("git", *arguments),
        cwd=repository,
        timeout=_GIT_CHECK_SECONDS,
    )


def _run_git_command(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    timeout: float,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProofGateExecutionError("proof gate failed") from exc


def _is_git_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )
