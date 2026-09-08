"""PostgreSQL integration tests for idempotent Check Run state changes."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from pr_reliability_api.db import apply_migrations
from pr_reliability_workers.activities import GitHubCheckRun, GitHubCheckRunOperation
from pr_reliability_workers.workflows.types import (
    CheckRunConclusion,
    CheckRunRequest,
    CheckRunStatus,
)
from psycopg import Connection

OWNER_ID = "01J00000000000000000000001"
REPOSITORY_ID = "01J00000000000000000000002"
PULL_REQUEST_ID = "01J00000000000000000000003"
RUN_ID = "01J00000000000000000000004"
NEXT_RUN_ID = "01J00000000000000000000005"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
NEXT_HEAD_SHA = "c" * 40
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def public_id(sequence: int) -> str:
    return f"01J{sequence:023d}"


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


class FakeCheckClient:
    def __init__(self) -> None:
        self.remote: GitHubCheckRun | None = None
        self.created: list[dict[str, object]] = []
        self.updated: list[dict[str, object]] = []

    async def find_check_run(
        self, repository: str, head_sha: str, external_id: str
    ) -> GitHubCheckRun | None:
        assert repository == "owner/repository"
        if self.remote is not None:
            assert self.remote.head_sha == head_sha
            assert self.remote.external_id == external_id
        return self.remote

    async def create_check_run(self, repository: str, payload: dict[str, object]) -> GitHubCheckRun:
        assert repository == "owner/repository"
        self.created.append(payload)
        self.remote = GitHubCheckRun(901, str(payload["head_sha"]), str(payload["external_id"]))
        return self.remote

    async def update_check_run(
        self, repository: str, remote_id: int, payload: dict[str, object]
    ) -> GitHubCheckRun:
        assert repository == "owner/repository"
        assert self.remote is not None and remote_id == self.remote.remote_id
        self.updated.append(payload)
        return self.remote


class CreateAcceptedThenFailedClient(FakeCheckClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def create_check_run(self, repository: str, payload: dict[str, object]) -> GitHubCheckRun:
        remote = await super().create_check_run(repository, payload)
        if not self.failed:
            self.failed = True
            raise RuntimeError("connection dropped after create")
        return remote


def seed_run(connection_factory: Callable[[], Connection[object]]) -> None:
    with connection_factory() as connection, connection.transaction():
        repository = connection.execute(
            """
            INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
            VALUES (%s, %s, 91, 'owner/repository') RETURNING id
            """,
            (REPOSITORY_ID, OWNER_ID),
        ).fetchone()[0]
        pull_request = connection.execute(
            """
            INSERT INTO pull_requests (
                public_id, owner_id, repository_id, github_number, base_sha, head_sha
            ) VALUES (%s, %s, %s, 17, %s, %s) RETURNING id
            """,
            (PULL_REQUEST_ID, OWNER_ID, repository, BASE_SHA, HEAD_SHA),
        ).fetchone()[0]
        run = connection.execute(
            """
            INSERT INTO runs (
                public_id, owner_id, pull_request_id, base_sha, head_sha, state,
                token_budget, cost_budget_usd_micros, generation
            ) VALUES (%s, %s, %s, %s, %s, 'queued', 100000, 750000, 1)
            RETURNING id
            """,
            (RUN_ID, OWNER_ID, pull_request, BASE_SHA, HEAD_SHA),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO findings (
                public_id, owner_id, run_id, finding_key, category, severity,
                claim, confidence, evidence
            ) VALUES
                (%s, %s, %s, 'safe', 'correctness', 'high', 'Unsafe access', 0.9,
                 '[{"kind":"source_location","summary":"unsafe",'
                 '"file_path":"apps/api/example.py","start_line":7,"end_line":8}]'::jsonb),
                (%s, %s, %s, 'unsafe', 'security', 'critical', 'Traversal', 0.9,
                 '[{"kind":"source_location","summary":"unsafe",'
                 '"file_path":"../secret","start_line":1}]'::jsonb)
            """,
            (public_id(10), OWNER_ID, run, public_id(11), OWNER_ID, run),
        )
        safe_finding = connection.execute(
            "SELECT id FROM findings WHERE run_id = %s AND finding_key = 'safe'",
            (run,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO approvals (
                public_id, owner_id, run_id, finding_id, actor_id,
                decision, head_sha, decided_at
            ) VALUES (%s, %s, %s, %s, %s, 'approved', %s, now())
            """,
            (public_id(12), OWNER_ID, run, safe_finding, public_id(13), HEAD_SHA),
        )


def request(
    status: CheckRunStatus,
    conclusion: CheckRunConclusion | None = None,
    *,
    run_id: str = RUN_ID,
    generation: int = 1,
    head_sha: str = HEAD_SHA,
) -> CheckRunRequest:
    return CheckRunRequest(
        owner_id=OWNER_ID,
        run_id=run_id,
        generation=generation,
        repository_id=REPOSITORY_ID,
        pull_request_number=17,
        head_sha=head_sha,
        status=status,
        conclusion=conclusion,
    )


def operation(
    connection_factory: Callable[[], Connection[object]], client: FakeCheckClient
) -> GitHubCheckRunOperation:
    values = iter(public_id(value) for value in range(20, 40))
    return GitHubCheckRunOperation(
        connection_factory,
        client,
        "https://reviews.example/dashboard",
        lambda: next(values),
        now=lambda: NOW,
    )


def test_transitions_once_and_sends_only_safe_annotations(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_run(connection_factory)
    client = FakeCheckClient()
    update = operation(connection_factory, client)

    asyncio.run(update(request(CheckRunStatus.QUEUED)))
    asyncio.run(update(request(CheckRunStatus.QUEUED)))
    asyncio.run(update(request(CheckRunStatus.IN_PROGRESS)))
    asyncio.run(update(request(CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS)))

    assert len(client.created) == 1
    assert [payload["status"] for payload in client.updated] == [
        "queued",
        "in_progress",
        "completed",
    ]
    output = client.updated[-1]["output"]
    assert isinstance(output, dict)
    assert output["annotations"] == [
        {
            "path": "apps/api/example.py",
            "start_line": 7,
            "end_line": 8,
            "annotation_level": "failure",
            "message": "Unsafe access",
            "title": "PR Reliability finding",
        }
    ]
    assert client.updated[-1]["details_url"].endswith(f"/dashboard?run={RUN_ID}")
    with connection_factory() as connection:
        stored = connection.execute(
            """
            SELECT remote_id, status, conclusion, current_run_id =
                   (SELECT id FROM runs WHERE public_id = %s)
            FROM github_check_runs
            """,
            (RUN_ID,),
        ).fetchone()
    assert stored == (901, "completed", "success", True)


@pytest.mark.parametrize(
    "conclusion",
    [
        CheckRunConclusion.FAILURE,
        CheckRunConclusion.ACTION_REQUIRED,
        CheckRunConclusion.CANCELLED,
        CheckRunConclusion.TIMED_OUT,
    ],
)
def test_non_success_conclusions_never_publish_finding_annotations(
    connection_factory: Callable[[], Connection[object]],
    conclusion: CheckRunConclusion,
) -> None:
    seed_run(connection_factory)
    client = FakeCheckClient()
    update = operation(connection_factory, client)

    asyncio.run(update(request(CheckRunStatus.QUEUED)))
    asyncio.run(update(request(CheckRunStatus.IN_PROGRESS)))
    asyncio.run(update(request(CheckRunStatus.COMPLETED, conclusion)))

    output = client.updated[-1]["output"]
    assert isinstance(output, dict)
    assert "annotations" not in output


def test_same_head_rerun_reuses_check_and_late_old_completion_is_ignored(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_run(connection_factory)
    client = FakeCheckClient()
    update = operation(connection_factory, client)
    asyncio.run(update(request(CheckRunStatus.QUEUED)))
    asyncio.run(update(request(CheckRunStatus.IN_PROGRESS)))
    asyncio.run(update(request(CheckRunStatus.COMPLETED, CheckRunConclusion.SUCCESS)))
    with connection_factory() as connection, connection.transaction():
        pull_request = connection.execute("SELECT id FROM pull_requests").fetchone()[0]
        connection.execute(
            """
            INSERT INTO runs (
                public_id, owner_id, pull_request_id, base_sha, head_sha, state,
                token_budget, cost_budget_usd_micros, generation
            ) VALUES (%s, %s, %s, %s, %s, 'queued', 100000, 750000, 2)
            """,
            (NEXT_RUN_ID, OWNER_ID, pull_request, BASE_SHA, HEAD_SHA),
        )

    asyncio.run(update(request(CheckRunStatus.QUEUED, run_id=NEXT_RUN_ID, generation=2)))
    calls_before_late_completion = len(client.updated)
    asyncio.run(update(request(CheckRunStatus.COMPLETED, CheckRunConclusion.FAILURE)))

    assert len(client.created) == 1
    assert len(client.updated) == calls_before_late_completion
    with connection_factory() as connection:
        current = connection.execute(
            """
            SELECT run.public_id, check_run.status, check_run.conclusion
            FROM github_check_runs AS check_run
            JOIN runs AS run ON run.id = check_run.current_run_id
            """
        ).fetchone()
    assert current == (NEXT_RUN_ID, "queued", None)


def test_superseded_queued_head_gets_cancelled_check(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_run(connection_factory)
    with connection_factory() as connection, connection.transaction():
        pull_request = connection.execute("SELECT id FROM pull_requests").fetchone()[0]
        connection.execute(
            "UPDATE pull_requests SET head_sha = %s WHERE id = %s",
            (NEXT_HEAD_SHA, pull_request),
        )
        connection.execute(
            """
            INSERT INTO runs (
                public_id, owner_id, pull_request_id, base_sha, head_sha, state,
                token_budget, cost_budget_usd_micros, generation
            ) VALUES (%s, %s, %s, %s, %s, 'queued', 100000, 750000, 2)
            """,
            (NEXT_RUN_ID, OWNER_ID, pull_request, BASE_SHA, NEXT_HEAD_SHA),
        )
    client = FakeCheckClient()
    update = operation(connection_factory, client)

    asyncio.run(update(request(CheckRunStatus.QUEUED)))
    asyncio.run(update(request(CheckRunStatus.IN_PROGRESS)))
    asyncio.run(update(request(CheckRunStatus.COMPLETED, CheckRunConclusion.CANCELLED)))

    assert len(client.created) == 1
    assert [payload["status"] for payload in client.updated] == [
        "queued",
        "in_progress",
        "completed",
    ]
    assert client.updated[-1]["conclusion"] == "cancelled"
    with connection_factory() as connection:
        stored = connection.execute(
            "SELECT head_sha, status, conclusion FROM github_check_runs"
        ).fetchone()
    assert stored == (HEAD_SHA, "completed", "cancelled")


def test_retry_recovers_remote_check_created_before_provider_failure(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_run(connection_factory)
    client = CreateAcceptedThenFailedClient()
    update = operation(connection_factory, client)

    with pytest.raises(RuntimeError, match="GitHub Check Run update failed"):
        asyncio.run(update(request(CheckRunStatus.QUEUED)))
    asyncio.run(update(request(CheckRunStatus.QUEUED)))

    assert len(client.created) == 1
    assert len(client.updated) == 1
    with connection_factory() as connection:
        stored = connection.execute("SELECT remote_id, status FROM github_check_runs").fetchone()
    assert stored == (901, "queued")
