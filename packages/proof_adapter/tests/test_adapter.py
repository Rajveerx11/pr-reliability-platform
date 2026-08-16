"""Contract tests for the local Proof of Work adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pr_reliability_proof_adapter import (
    PROOF_VERDICT_VERSION,
    ProofAdapter,
    ProofGateExecutionError,
    ProofGateResult,
    ProofGateTimeoutError,
    ProofRequest,
)


class StaticRunner:
    def __init__(self, result: ProofGateResult) -> None:
        self.result = result

    async def run(self, request: ProofRequest) -> ProofGateResult:
        del request
        return self.result


class ErrorRunner:
    async def run(self, request: ProofRequest) -> ProofGateResult:
        del request
        raise RuntimeError("package internals must not escape")


class SlowRunner:
    async def run(self, request: ProofRequest) -> ProofGateResult:
        del request
        await asyncio.sleep(10)
        raise AssertionError("timeout did not cancel the gate")


def test_pass_returns_small_versioned_verdict(tmp_path: Path) -> None:
    adapter = ProofAdapter(
        StaticRunner(
            ProofGateResult(
                "0.2.0",
                {
                    "passed": True,
                    "reasons": ["no cheat signals; facts check out"],
                    "findings": [],
                    "tests": {"ran": False},
                },
            )
        )
    )

    verdict = asyncio.run(adapter.verify(ProofRequest(tmp_path)))

    assert verdict.version == PROOF_VERDICT_VERSION
    assert verdict.passed
    assert verdict.reasons == ("no cheat signals; facts check out",)
    assert verdict.finding_rules == ()
    assert verdict.package_version == "0.2.0"


def test_fail_preserves_reasons_and_unique_rules(tmp_path: Path) -> None:
    adapter = ProofAdapter(
        StaticRunner(
            ProofGateResult(
                "0.2.0",
                {
                    "passed": False,
                    "reasons": ["BLOCK fake-pass: hard exit"],
                    "findings": [
                        {"rule": "fake-pass:sys-exit"},
                        {"rule": "fake-pass:sys-exit"},
                    ],
                },
            )
        )
    )

    verdict = asyncio.run(adapter.verify(ProofRequest(tmp_path)))

    assert not verdict.passed
    assert verdict.reasons == ("BLOCK fake-pass: hard exit",)
    assert verdict.finding_rules == ("fake-pass:sys-exit",)


def test_timeout_fails_without_a_verdict(tmp_path: Path) -> None:
    adapter = ProofAdapter(SlowRunner())

    with pytest.raises(ProofGateTimeoutError, match="timed out"):
        asyncio.run(adapter.verify(ProofRequest(tmp_path, timeout_seconds=0.01)))


def test_gate_error_fails_without_leaking_package_details(tmp_path: Path) -> None:
    adapter = ProofAdapter(ErrorRunner())

    with pytest.raises(ProofGateExecutionError, match="^proof gate failed$"):
        asyncio.run(adapter.verify(ProofRequest(tmp_path)))


def test_published_gate_rejects_a_non_git_directory(tmp_path: Path) -> None:
    with pytest.raises(ProofGateExecutionError, match="not a Git checkout"):
        asyncio.run(ProofAdapter().verify(ProofRequest(tmp_path)))


@pytest.mark.parametrize(
    "result",
    [
        ProofGateResult("0.2.0", {"passed": "yes", "reasons": [], "findings": []}),
        ProofGateResult("0.2.0", {"passed": True, "reasons": "ok", "findings": []}),
        ProofGateResult("0.2.0", {"passed": True, "reasons": [], "findings": [{}]}),
        ProofGateResult("", {"passed": True, "reasons": [], "findings": []}),
    ],
)
def test_malformed_result_fails_without_a_verdict(tmp_path: Path, result: ProofGateResult) -> None:
    adapter = ProofAdapter(StaticRunner(result))

    with pytest.raises(ProofGateExecutionError, match="malformed"):
        asyncio.run(adapter.verify(ProofRequest(tmp_path)))
