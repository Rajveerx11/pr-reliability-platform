"""Contract tests for the local Proof of Work adapter."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pr_reliability_proof_adapter.adapter as adapter_module
import pytest
from pr_reliability_proof_adapter import (
    PROOF_VERDICT_VERSION,
    ProofAdapter,
    ProofGateExecutionError,
    ProofGateResult,
    ProofGateTimeoutError,
    ProofRequest,
    PublishedProofGate,
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


def test_published_gate_runs_real_package_and_cleans_isolated_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base_sha = _repository_with_change(tmp_path)
    created_directories: list[Path] = []
    temporary_directory = adapter_module.tempfile.TemporaryDirectory

    def tracking_temporary_directory(*args, **kwargs):
        directory = temporary_directory(*args, **kwargs)
        created_directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(
        adapter_module.tempfile,
        "TemporaryDirectory",
        tracking_temporary_directory,
    )

    verdict = asyncio.run(ProofAdapter().verify(ProofRequest(repository, base_ref=base_sha)))

    assert verdict.passed
    assert not (repository / ".proofofwork").exists()
    assert created_directories and all(not path.exists() for path in created_directories)


def test_published_gate_rejects_missing_base_and_empty_diff(tmp_path: Path) -> None:
    repository, _ = _repository_with_change(tmp_path)

    with pytest.raises(ProofGateExecutionError, match="^proof gate failed$"):
        asyncio.run(ProofAdapter().verify(ProofRequest(repository, base_ref="c" * 40)))
    with pytest.raises(ProofGateExecutionError, match="^proof gate failed$"):
        asyncio.run(ProofAdapter().verify(ProofRequest(repository, base_ref="HEAD")))


def test_published_gate_timeout_kills_process_tree(tmp_path: Path) -> None:
    repository, base_sha = _repository_with_change(tmp_path)
    marker = tmp_path / "escaped-child"
    child = (
        "import pathlib,time; time.sleep(2); "
        f"pathlib.Path({str(marker)!r}).write_text('alive', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
    )
    adapter = ProofAdapter(PublishedProofGate((sys.executable, "-c", parent)))
    started_at = time.monotonic()

    with pytest.raises(ProofGateTimeoutError, match="timed out"):
        asyncio.run(
            adapter.verify(ProofRequest(repository, base_ref=base_sha, timeout_seconds=0.5))
        )

    assert time.monotonic() - started_at < 2
    time.sleep(2)
    assert not marker.exists()


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


def _repository_with_change(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "proof-test@example.invalid")
    _git(repository, "config", "user.name", "Proof Test")
    source = repository / "example.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    _git(repository, "add", "example.py")
    _git(repository, "commit", "--quiet", "-m", "base")
    base_sha = _git(repository, "rev-parse", "HEAD").stdout.strip()
    source.write_text("def value():\n    return 2\n", encoding="utf-8")
    _git(repository, "add", "example.py")
    _git(repository, "commit", "--quiet", "-m", "change")
    return repository, base_sha


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    )
