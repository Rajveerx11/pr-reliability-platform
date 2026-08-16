"""Translate Proof of Work results into a small platform-owned verdict."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

PROOF_VERDICT_VERSION = 1
_MAX_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class ProofRequest:
    """Inputs needed to inspect one reviewed repository without running its tests."""

    repository: Path
    base_ref: str = "HEAD"
    timeout_seconds: float = 60
    log_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path):
            raise TypeError("proof repository must be a Path")
        if not isinstance(self.base_ref, str) or not self.base_ref or "\x00" in self.base_ref:
            raise ValueError("proof base_ref must be a non-empty string without NUL")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("proof timeout_seconds must be finite and positive")
        if self.log_path is not None and not isinstance(self.log_path, Path):
            raise TypeError("proof log_path must be a Path")


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
    """Only code allowed to import and call the published package."""

    async def run(self, request: ProofRequest) -> ProofGateResult:
        if not request.repository.is_dir() or not (request.repository / ".git").exists():
            raise ProofGateExecutionError("proof repository is not a Git checkout")

        from proofofwork import __version__
        from proofofwork.engine import check

        verdict = await asyncio.to_thread(
            check,
            root=str(request.repository),
            base_ref=request.base_ref,
            run_tests=False,
            run_mutation=False,
            use_judge=False,
            db_path=str(request.log_path) if request.log_path is not None else None,
        )
        return ProofGateResult(package_version=__version__, payload=verdict.as_dict())


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
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise TypeError("reasons must be a list of strings")
    if not isinstance(findings, list):
        raise TypeError("findings must be a list")

    finding_rules: list[str] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            raise TypeError("each finding must be a mapping")
        rule = finding["rule"]
        if not isinstance(rule, str) or not rule:
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
