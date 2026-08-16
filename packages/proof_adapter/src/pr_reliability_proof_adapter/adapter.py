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


@dataclass(frozen=True)
class ProofRequest:
    """Inputs needed to inspect one reviewed repository without running its tests."""

    repository: Path
    base_ref: str = "HEAD"
    timeout_seconds: float = 60

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path):
            raise TypeError("proof repository must be a Path")
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
        self._worker_command = worker_command or (
            sys.executable,
            "-m",
            "pr_reliability_proof_adapter._published_gate",
        )
        if not self._worker_command or any(not item for item in self._worker_command):
            raise ValueError("proof worker command must contain non-empty arguments")

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
            log_path = trusted_directory / "log.db"
            if log_path.exists() or log_path.is_symlink():
                raise ProofGateExecutionError("proof log path is not isolated")

            process = await _start_worker(
                self._worker_command,
                repository,
                request.base_ref,
                log_path,
            )
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    stdout, stderr = await _collect_bounded_output(process)
            except TimeoutError as exc:
                await _kill_process_tree(process)
                raise ProofGateTimeoutError("proof gate timed out") from exc
            except asyncio.CancelledError:
                await _kill_process_tree(process)
                raise
            except Exception:
                await _kill_process_tree(process)
                raise

            if process.returncode != 0:
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

    async def verify(self, request: ProofRequest) -> ProofVerdict:
        try:
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
) -> asyncio.subprocess.Process:
    options: dict[str, object] = {}
    if os.name == "posix":
        options["start_new_session"] = True
    elif os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return await asyncio.create_subprocess_exec(
            *worker_command,
            str(repository),
            base_ref,
            str(log_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **options,
        )
    except OSError as exc:
        raise ProofGateExecutionError("proof gate failed") from exc


async def _collect_bounded_output(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise ProofGateExecutionError("proof gate failed")
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, _MAX_GATE_OUTPUT_BYTES))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, _MAX_GATE_ERROR_BYTES))
    wait_task = asyncio.create_task(process.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        stdout, stderr, _ = await asyncio.gather(*tasks)
        return stdout, stderr
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


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        elif os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=5)
            except (OSError, TimeoutError):
                with suppress(ProcessLookupError):
                    process.kill()
        else:
            with suppress(ProcessLookupError):
                process.kill()
    with suppress(ProcessLookupError):
        await process.wait()
    streams = tuple(stream for stream in (process.stdout, process.stderr) if stream is not None)
    await asyncio.gather(*(stream.read() for stream in streams), return_exceptions=True)
