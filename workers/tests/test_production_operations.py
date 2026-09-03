"""Integration coverage for production activity persistence without live providers."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from pr_reliability_api.db import apply_migrations
from pr_reliability_contracts import ModelUsage as ContractUsage
from pr_reliability_contracts import UsageCoverage
from pr_reliability_proof_adapter import ProofVerdict
from pr_reliability_workers.activities import VerificationEvidence
from pr_reliability_workers.agents import ModelRequest, ModelResponse, ReviewAgent
from pr_reliability_workers.providers.operations import (
    ProductionOperations,
    _git_bytes,
    _git_environment,
    _run_bounded_process,
    _usage_data,
    _usage_from_data,
)
from pr_reliability_workers.sandbox import SandboxResult
from pr_reliability_workers.workflows.types import (
    ModelUsage,
    StageRequest,
    TerminalRequest,
    WorkflowOutcome,
)
from psycopg import Connection

OWNER_ID = "01J00000000000000000000001"
REPOSITORY_ID = "01J00000000000000000000002"
PULL_REQUEST_ID = "01J00000000000000000000003"
RUN_ID = "01J00000000000000000000004"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
SANDBOX_IMAGE = f"sha256:{'c' * 64}"


def test_usage_receipt_round_trip_preserves_total_tokens() -> None:
    usage = ModelUsage(
        input_tokens=12,
        output_tokens=5,
        cost_usd_micros=None,
        total_tokens=17,
    )

    assert _usage_from_data(_usage_data(usage)) == usage


def test_bounded_process_rejects_combined_output_and_reaps_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run():
        original = asyncio.create_subprocess_exec
        started = []

        async def capture(*arguments, **options):
            process = await original(*arguments, **options)
            started.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        script = (
            "import sys,time;"
            "sys.stdout.write('PRIVATE_STDOUT_' + 'x' * 32);sys.stdout.flush();"
            "sys.stderr.write('PRIVATE_STDERR_' + 'x' * 32);sys.stderr.flush();"
            "time.sleep(60)"
        )
        with pytest.raises(RuntimeError, match="Git repository inspection failed") as raised:
            await _run_bounded_process(
                sys.executable,
                ("-c", script),
                cwd=tmp_path,
                environment=_git_environment(),
                timeout_seconds=5,
                output_limit_bytes=64,
            )
        return raised.value, started

    error, started = asyncio.run(run())

    assert len(started) == 1
    assert started[0].returncode is not None
    assert "PRIVATE_STDOUT" not in repr(error)
    assert "PRIVATE_STDERR" not in repr(error)


def test_bounded_process_times_out_and_reaps_child_without_leaking_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run():
        original = asyncio.create_subprocess_exec
        started = []

        async def capture(*arguments, **options):
            process = await original(*arguments, **options)
            started.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        script = "import time;print('PRIVATE_TIMEOUT_SENTINEL', flush=True);time.sleep(60)"
        with pytest.raises(RuntimeError, match="Git repository inspection failed") as raised:
            await _run_bounded_process(
                sys.executable,
                ("-c", script),
                cwd=tmp_path,
                environment=_git_environment(),
                timeout_seconds=0.5,
                output_limit_bytes=1024,
            )
        return raised.value, started

    error, started = asyncio.run(run())

    assert len(started) == 1
    assert started[0].returncode is not None
    assert "PRIVATE_TIMEOUT_SENTINEL" not in repr(error)


def test_git_inspection_returns_normal_bounded_output(tmp_path: Path) -> None:
    output = asyncio.run(_git_bytes(tmp_path, "--version"))

    assert output.startswith(b"git version ")


@pytest.fixture
def connection_factory() -> Iterator[Callable[[], Connection[object]]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        if os.environ.get("CI"):
            pytest.fail("CI must provide TEST_DATABASE_URL")
        pytest.skip("TEST_DATABASE_URL is required")
    schema = f"test_{uuid4().hex}"
    with psycopg.connect(database_url) as setup:
        setup.execute(f'CREATE SCHEMA "{schema}"')
        setup.execute(f'SET search_path TO "{schema}"')
        setup.commit()
        apply_migrations(setup)

    def create() -> Connection[object]:
        connection = psycopg.connect(database_url)
        connection.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    try:
        yield create
    finally:
        with psycopg.connect(database_url) as cleanup:
            cleanup.execute(f'DROP SCHEMA "{schema}" CASCADE')


class FakeModelClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        assert request.idempotency_key == f"{RUN_ID}:{HEAD_SHA}:analyze"
        return ModelResponse(
            output_json=json.dumps(
                {
                    "findings": [
                        {
                            "category": "correctness",
                            "severity": "high",
                            "claim": "The changed function returns the wrong value.",
                            "confidence": 0.95,
                            "evidence": [
                                {
                                    "schema_version": "1",
                                    "kind": "source_location",
                                    "summary": "The return value changed.",
                                    "file_path": "example.py",
                                    "start_line": 1,
                                }
                            ],
                        }
                    ]
                }
            ),
            usage=ContractUsage(
                schema_version="1",
                coverage=UsageCoverage.FULL,
                prompt_tokens=12,
                completion_tokens=5,
                total_tokens=17,
            ),
        )


@dataclass(frozen=True)
class FakeCheckoutResult:
    workspace: Path
    base_sha: str
    head_sha: str
    reference: str


class LocalFixtureCheckout:
    def __init__(self, source: Path, staging: Path, actual_base: str, actual_head: str) -> None:
        self.source = source
        self.staging = staging
        self.actual_base = actual_base
        self.actual_head = actual_head
        self.calls = 0

    async def checkout(
        self,
        repository_full_name: str,
        repository_id: int,
        pull_request_number: int,
        base_sha: str,
        head_sha: str,
        idempotency_key: str,
    ) -> FakeCheckoutResult:
        assert (repository_full_name, repository_id, pull_request_number) == (
            "owner/repository",
            123,
            17,
        )
        assert (base_sha, head_sha) == (self.actual_base, self.actual_head)
        assert idempotency_key == f"{RUN_ID}:{HEAD_SHA}:select_context"
        self.calls += 1
        target = self.staging / f"fixture-checkout-{self.calls}"
        await asyncio.to_thread(
            subprocess.run,
            ("git", "clone", "--quiet", "--no-local", str(self.source), str(target)),
            check=True,
            capture_output=True,
        )
        await asyncio.to_thread(
            subprocess.run,
            ("git", "checkout", "--quiet", "--detach", self.actual_head),
            cwd=target,
            check=True,
            capture_output=True,
        )
        return FakeCheckoutResult(target, base_sha, head_sha, "safe-checkout-ref")


def _fixture_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "fixture-source"
    repository.mkdir()
    subprocess.run(("git", "init", "--quiet"), cwd=repository, check=True)
    subprocess.run(("git", "config", "user.name", "Test"), cwd=repository, check=True)
    subprocess.run(
        ("git", "config", "user.email", "test@example.invalid"), cwd=repository, check=True
    )
    (repository / "example.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    subprocess.run(("git", "add", "example.py"), cwd=repository, check=True)
    subprocess.run(("git", "commit", "--quiet", "-m", "base"), cwd=repository, check=True)
    base = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repository / "example.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    subprocess.run(("git", "commit", "--quiet", "-am", "head"), cwd=repository, check=True)
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repository, base, head


def _seed(connection_factory, base: str, head: str) -> None:
    with connection_factory() as connection:
        repository_id = connection.execute(
            """
            INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
            VALUES (%s, %s, 123, 'owner/repository') RETURNING id
            """,
            (REPOSITORY_ID, OWNER_ID),
        ).fetchone()[0]
        pull_request_id = connection.execute(
            """
            INSERT INTO pull_requests (
                public_id, owner_id, repository_id, github_number, base_sha, head_sha
            ) VALUES (%s, %s, %s, 17, %s, %s) RETURNING id
            """,
            (PULL_REQUEST_ID, OWNER_ID, repository_id, base, head),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO runs (
                public_id, owner_id, pull_request_id, base_sha, head_sha,
                token_budget, cost_budget_usd_micros
            ) VALUES (%s, %s, %s, %s, %s, 1000, 1000000)
            """,
            (RUN_ID, OWNER_ID, pull_request_id, base, head),
        )
        connection.commit()


def _request(step: str, *, input_ref: str | None = None, base_sha: str | None = None):
    return StageRequest(
        owner_id=OWNER_ID,
        run_id=RUN_ID,
        head_sha=HEAD_SHA,
        base_sha=base_sha,
        input_ref=input_ref,
        idempotency_key=f"{RUN_ID}:{HEAD_SHA}:{step}",
    )


def test_operations_persist_only_safe_receipts_and_replay_analysis(
    connection_factory,
    tmp_path: Path,
) -> None:
    async def run() -> None:
        source, actual_base, actual_head = _fixture_repository(tmp_path)
        staging = tmp_path / "staging"
        staging.mkdir()
        _seed(connection_factory, actual_base, actual_head)
        checkout = LocalFixtureCheckout(source, staging, actual_base, actual_head)
        model = FakeModelClient()
        operations = ProductionOperations(
            connection_factory=connection_factory,
            checkout=checkout,
            reviewer=ReviewAgent(model),
            workspace_root=staging,
            sandbox_image=SANDBOX_IMAGE,
            sandbox_command=("python", "-m", "pytest", "-q"),
            id_factory=lambda: f"01J{uuid4().int % 10**23:023d}",
        )
        global BASE_SHA, HEAD_SHA
        BASE_SHA, HEAD_SHA = actual_base, actual_head

        context = await operations.select_context(_request("select_context", base_sha=actual_base))
        analysis_request = _request(
            "analyze",
            base_sha=actual_base,
            input_ref=context.output_ref,
        )
        analysis = await operations.analyze(analysis_request)
        replay = await operations.analyze(analysis_request)

        assert analysis == replay
        assert model.calls == 1
        assert analysis.usage == ModelUsage(input_tokens=12, output_tokens=5, total_tokens=17)
        await operations.prepare_verification(
            _request("verify", base_sha=actual_base, input_ref=analysis.output_ref)
        )
        await operations.record_verification(
            _request("verify", base_sha=actual_base, input_ref=analysis.output_ref),
            VerificationEvidence(
                sandbox=SandboxResult(
                    exit_code=0,
                    stdout="private sandbox output",
                    stderr="private sandbox error",
                    duration_ms=10,
                ),
                proof=ProofVerdict(1, True, ("private proof reason",), ("rule-a",), "0.2.0"),
            ),
        )
        await operations.record_terminal(
            TerminalRequest(
                owner_id=OWNER_ID,
                run_id=RUN_ID,
                head_sha=actual_head,
                outcome=WorkflowOutcome.REJECTED,
                reason="user supplied secret cancellation reason",
                idempotency_key=f"{RUN_ID}:{actual_head}:terminal:rejected",
                usage=analysis.usage,
            )
        )

        with pytest.raises(RuntimeError, match="review run is terminal"):
            operations._record_stage(
                _request("verify", base_sha=actual_base, input_ref=analysis.output_ref),
                {"output_ref": f"verification:{RUN_ID}:{actual_head}"},
                None,
            )
        with pytest.raises(RuntimeError, match="review run is terminal"):
            operations._record_analysis(
                analysis_request,
                (),
                {"output_ref": analysis.output_ref, "finding_ids": [], "usage": None},
                ModelUsage(),
            )

        with connection_factory() as connection:
            finding_count = connection.execute("SELECT count(*) FROM findings").fetchone()[0]
            state = connection.execute(
                "SELECT state FROM runs WHERE public_id = %s", (RUN_ID,)
            ).fetchone()[0]
            events = connection.execute(
                "SELECT event_data::text FROM run_events ORDER BY id"
            ).fetchall()
        persisted = "\n".join(row[0] for row in events)
        assert finding_count == 1
        assert state == "rejected"
        assert "private source" not in persisted
        assert "private sandbox output" not in persisted
        assert "private sandbox error" not in persisted
        assert "private proof reason" not in persisted
        assert "user supplied secret" not in persisted
        assert '"reason_code": "cancelled"' in persisted
        assert '"total_tokens": 17' in persisted
        assert not any(staging.iterdir())

    asyncio.run(run())
