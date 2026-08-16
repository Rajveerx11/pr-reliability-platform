"""Integration tests for approval-bound GitHub comment publishing."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from pr_reliability_api.db import apply_migrations
from pr_reliability_workers.activities import (
    GitHubComment,
    GitHubCommentPayloadMismatch,
    GitHubCommentPublishOperation,
)
from pr_reliability_workers.workflows.types import PublishRequest
from psycopg import Connection
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
from temporalio.exceptions import ApplicationError

OWNER_ID = "01J00000000000000000000001"
REPOSITORY_ID = "01J00000000000000000000002"
PULL_REQUEST_ID = "01J00000000000000000000003"
RUN_ID = "01J00000000000000000000004"
FINDING_ID = "01J00000000000000000000005"
APPROVAL_ID = "01J00000000000000000000006"
ACTOR_ID = "01J00000000000000000000007"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
NEXT_HEAD_SHA = "c" * 40


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


class FakeGitHubClient:
    def __init__(self, *, current_head: str = HEAD_SHA) -> None:
        self.current_head = current_head
        self.comments: list[tuple[GitHubComment, str]] = []
        self.create_calls = 0
        self.find_calls = 0

    async def current_head_sha(self, repository: str, pull_request_number: int) -> str:
        assert repository == "owner/repository"
        assert pull_request_number == 17
        return self.current_head

    async def find_comment(
        self,
        repository: str,
        pull_request_number: int,
        marker: str,
        expected_body: str,
    ) -> GitHubComment | None:
        assert repository == "owner/repository"
        assert pull_request_number == 17
        self.find_calls += 1
        for comment, body in self.comments:
            if body == expected_body:
                return comment
        if any(body.endswith(f"\n\n{marker}") for _, body in self.comments):
            raise GitHubCommentPayloadMismatch
        return None

    async def create_comment(
        self,
        repository: str,
        pull_request_number: int,
        body: str,
    ) -> GitHubComment:
        assert repository == "owner/repository"
        assert pull_request_number == 17
        self.create_calls += 1
        comment = GitHubComment(str(1000 + self.create_calls))
        self.comments.append((comment, body))
        return comment


class BlockingGitHubClient(FakeGitHubClient):
    def __init__(self, create_barrier: asyncio.Barrier, release_create: asyncio.Event) -> None:
        super().__init__()
        self.create_barrier = create_barrier
        self.release_create = release_create

    async def create_comment(
        self,
        repository: str,
        pull_request_number: int,
        body: str,
    ) -> GitHubComment:
        await self.create_barrier.wait()
        await self.release_create.wait()
        return await super().create_comment(repository, pull_request_number, body)


class LeakyGitHubClient(FakeGitHubClient):
    async def find_comment(
        self,
        repository: str,
        pull_request_number: int,
        marker: str,
        expected_body: str,
    ) -> GitHubComment | None:
        del repository, pull_request_number, marker, expected_body
        raise RuntimeError("SECRET_GITHUB_RESPONSE_BODY")


class SimulatedWorkerCrash(BaseException):
    """Stop the activity after GitHub accepts a comment but before its receipt."""


class CrashAfterCreateGitHubClient(FakeGitHubClient):
    def __init__(self, *, current_head: str = HEAD_SHA) -> None:
        super().__init__(current_head=current_head)
        self.crashed = False

    async def create_comment(
        self,
        repository: str,
        pull_request_number: int,
        body: str,
    ) -> GitHubComment:
        comment = await super().create_comment(repository, pull_request_number, body)
        if not self.crashed:
            self.crashed = True
            raise SimulatedWorkerCrash
        return comment


def seed_review(
    connection_factory: Callable[[], Connection[object]],
    *,
    decision: str | None = "approved",
    current_head: str = HEAD_SHA,
) -> None:
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
            (PULL_REQUEST_ID, OWNER_ID, repository, BASE_SHA, current_head),
        ).fetchone()[0]
        run = connection.execute(
            """
            INSERT INTO runs (
                public_id, owner_id, pull_request_id, base_sha, head_sha, state,
                token_budget, cost_budget_usd_micros, generation
            ) VALUES (%s, %s, %s, %s, %s, 'awaiting_approval', 100000, 750000, 1)
            RETURNING id
            """,
            (RUN_ID, OWNER_ID, pull_request, BASE_SHA, HEAD_SHA),
        ).fetchone()[0]
        finding = connection.execute(
            """
            INSERT INTO findings (
                public_id, owner_id, run_id, finding_key, category, severity,
                claim, confidence, evidence
            ) VALUES (%s, %s, %s, 'finding-1', 'correctness', 'high',
                      'Null input crashes the request', 0.95,
                      '[{"schema_version":"1","kind":"source_location",'
                      '"summary":"Unsafe access","file_path":"apps/api/example.py"}]'::jsonb)
            RETURNING id
            """,
            (FINDING_ID, OWNER_ID, run),
        ).fetchone()[0]
        if decision is not None:
            connection.execute(
                """
                INSERT INTO approvals (
                    public_id, owner_id, run_id, finding_id, actor_id,
                    decision, reason, head_sha, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'reviewed', %s, %s)
                """,
                (
                    APPROVAL_ID,
                    OWNER_ID,
                    run,
                    finding,
                    ACTOR_ID,
                    decision,
                    HEAD_SHA,
                    datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
                ),
            )


def publish_request() -> PublishRequest:
    return PublishRequest(
        owner_id=OWNER_ID,
        run_id=RUN_ID,
        repository_id=REPOSITORY_ID,
        pull_request_number=17,
        head_sha=HEAD_SHA,
        finding_ids=(FINDING_ID,),
        approval_ids=(APPROVAL_ID,),
        comment_body_ref="approved-findings",
        idempotency_key=f"{RUN_ID}:{HEAD_SHA}:publish",
    )


def publisher(
    connection_factory: Callable[[], Connection[object]],
    client: FakeGitHubClient,
) -> GitHubCommentPublishOperation:
    ids = iter(public_id(value) for value in range(20, 100))
    return GitHubCommentPublishOperation(
        connection_factory,
        client,
        id_factory=lambda: next(ids),
        now=lambda: datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
    )


def test_unapproved_finding_cannot_publish(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory, decision=None)
    github = FakeGitHubClient()

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(publish_request()))

    assert raised.value.type == "PublishBlocked"
    assert raised.value.non_retryable
    assert github.comments == []
    with connection_factory() as connection:
        assert connection.execute("SELECT count(*) FROM external_actions").fetchone()[0] == 0


def test_stable_retry_creates_one_comment_and_safe_audit(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = FakeGitHubClient()
    operation = publisher(connection_factory, github)

    asyncio.run(operation(publish_request()))
    asyncio.run(operation(publish_request()))

    assert github.create_calls == 1
    assert len(github.comments) == 1
    assert "Null input crashes the request" in github.comments[0][1]
    with connection_factory() as connection:
        action = connection.execute(
            "SELECT status, remote_id, payload_fingerprint FROM external_actions"
        ).fetchone()
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action[:2] == ("published", "1001")
    assert len(action[2]) == 64
    assert event[0] == "github.comment_published"
    assert event[1] == {
        "action_id": public_id(20),
        "remote_comment_id": "1001",
        "head_sha": HEAD_SHA,
        "finding_ids": [FINDING_ID],
        "approval_ids": [APPROVAL_ID],
        "payload_fingerprint": action[2],
    }
    assert "Null input" not in str(event[1])


def test_concurrent_retries_hold_one_publish_claim(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)

    async def scenario() -> BlockingGitHubClient:
        create_barrier = asyncio.Barrier(2)
        release_create = asyncio.Event()
        github = BlockingGitHubClient(create_barrier, release_create)
        operation = publisher(connection_factory, github)
        first = asyncio.create_task(operation(publish_request()))
        await create_barrier.wait()
        second = asyncio.create_task(operation(publish_request()))
        await asyncio.sleep(0.1)
        assert github.find_calls == 1
        assert not second.done()
        release_create.set()
        await asyncio.gather(first, second)
        return github

    github = asyncio.run(scenario())

    assert github.create_calls == 1
    assert len(github.comments) == 1
    with connection_factory() as connection:
        assert connection.execute("SELECT status, remote_id FROM external_actions").fetchone() == (
            "published",
            "1001",
        )
        assert connection.execute("SELECT count(*) FROM run_events").fetchone()[0] == 1


def test_retry_recovers_comment_created_before_database_receipt(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    request = publish_request()
    github = CrashAfterCreateGitHubClient()
    operation = publisher(connection_factory, github)

    with pytest.raises(SimulatedWorkerCrash):
        asyncio.run(operation(request))

    asyncio.run(operation(request))

    assert github.create_calls == 1
    with connection_factory() as connection:
        assert connection.execute("SELECT status, remote_id FROM external_actions").fetchone() == (
            "published",
            "1001",
        )


def test_retry_reconciles_created_comment_after_head_advances(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    request = publish_request()
    github = CrashAfterCreateGitHubClient()
    operation = publisher(connection_factory, github)

    with pytest.raises(SimulatedWorkerCrash):
        asyncio.run(operation(request))

    with connection_factory() as connection, connection.transaction():
        connection.execute(
            "UPDATE pull_requests SET head_sha = %s WHERE public_id = %s",
            (NEXT_HEAD_SHA, PULL_REQUEST_ID),
        )

    github.current_head = NEXT_HEAD_SHA
    asyncio.run(operation(request))

    assert github.create_calls == 1
    with connection_factory() as connection:
        assert connection.execute("SELECT status, remote_id FROM external_actions").fetchone() == (
            "published",
            "1001",
        )
        assert connection.execute("SELECT event_type FROM run_events").fetchone()[0] == (
            "github.comment_published"
        )


def test_published_retry_with_different_payload_fails_closed(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = FakeGitHubClient()
    operation = publisher(connection_factory, github)
    request = publish_request()
    asyncio.run(operation(request))

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(operation(replace(request, comment_body_ref="different-approved-body")))

    assert raised.value.type == "PublishBlocked"
    assert github.create_calls == 1
    assert github.find_calls == 1
    with connection_factory() as connection:
        assert connection.execute("SELECT status, remote_id FROM external_actions").fetchone() == (
            "published",
            "1001",
        )


def test_crash_recovery_with_different_payload_fails_before_marker_lookup(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = CrashAfterCreateGitHubClient()
    operation = publisher(connection_factory, github)
    request = publish_request()

    with pytest.raises(SimulatedWorkerCrash):
        asyncio.run(operation(request))
    with pytest.raises(ApplicationError) as raised:
        asyncio.run(operation(replace(request, comment_body_ref="different-approved-body")))

    assert raised.value.type == "PublishBlocked"
    assert github.create_calls == 1
    assert github.find_calls == 1
    with connection_factory() as connection:
        assert connection.execute("SELECT status, remote_id FROM external_actions").fetchone() == (
            "publishing",
            None,
        )


def test_crash_recovery_rejects_edited_comment_with_same_terminal_marker(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = CrashAfterCreateGitHubClient()
    operation = publisher(connection_factory, github)
    request = publish_request()

    with pytest.raises(SimulatedWorkerCrash):
        asyncio.run(operation(request))
    comment, original_body = github.comments[0]
    marker = original_body.rsplit("\n\n", 1)[1]
    github.comments[0] = (comment, f"edited after publication\n\n{marker}")

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(operation(request))

    assert raised.value.type == "PublishBlocked"
    assert raised.value.non_retryable
    assert github.create_calls == 1
    with connection_factory() as connection:
        action = connection.execute("SELECT status, remote_id FROM external_actions").fetchone()
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action == ("failed", None)
    assert event == (
        "github.comment_publish_failed",
        {
            "action_id": public_id(20),
            "head_sha": HEAD_SHA,
            "failure_code": "comment_payload_mismatch",
        },
    )


def test_provider_failure_is_sanitized_before_temporal_conversion(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(publisher(connection_factory, LeakyGitHubClient())(publish_request()))

    assert str(raised.value) == "GitHub comment publish failed"
    assert raised.value.__cause__ is None
    failure = Failure()
    DefaultFailureConverter.default.to_failure(
        raised.value,
        DefaultPayloadConverter.default,
        failure,
    )
    assert "SECRET_GITHUB_RESPONSE_BODY" not in str(failure)


def test_database_stale_head_blocks_before_github(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory, current_head=NEXT_HEAD_SHA)
    github = FakeGitHubClient()

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(publish_request()))

    assert raised.value.type == "PublishBlocked"
    assert github.comments == []


def test_remote_stale_head_blocks_and_records_safe_failure(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = FakeGitHubClient(current_head=NEXT_HEAD_SHA)

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(publish_request()))

    assert raised.value.type == "PublishBlocked"
    assert github.comments == []
    with connection_factory() as connection:
        action = connection.execute("SELECT status FROM external_actions").fetchone()[0]
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action == "failed"
    assert event == (
        "github.comment_publish_failed",
        {
            "action_id": public_id(20),
            "head_sha": HEAD_SHA,
            "failure_code": "stale_head",
        },
    )
