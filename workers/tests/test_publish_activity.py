"""Integration tests for approval-bound GitHub comment publishing."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from pr_reliability_api.db import apply_migrations
from pr_reliability_workers.activities import GitHubComment, GitHubCommentPublishOperation
from pr_reliability_workers.workflows.types import PublishRequest
from psycopg import Connection
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

    async def current_head_sha(self, repository: str, pull_request_number: int) -> str:
        assert repository == "owner/repository"
        assert pull_request_number == 17
        return self.current_head

    async def find_comment(
        self,
        repository: str,
        pull_request_number: int,
        marker: str,
    ) -> GitHubComment | None:
        assert repository == "owner/repository"
        assert pull_request_number == 17
        return next((comment for comment, body in self.comments if marker in body), None)

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
        action = connection.execute("SELECT status, remote_id FROM external_actions").fetchone()
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action == ("published", "1001")
    assert event[0] == "github.comment_published"
    assert event[1] == {
        "action_id": public_id(20),
        "remote_comment_id": "1001",
        "head_sha": HEAD_SHA,
        "finding_ids": [FINDING_ID],
        "approval_ids": [APPROVAL_ID],
    }
    assert "Null input" not in str(event[1])


def test_retry_recovers_comment_created_before_database_receipt(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    request = publish_request()
    marker = (
        "<!-- pr-reliability:"
        + hashlib.sha256(request.idempotency_key.encode()).hexdigest()
        + " -->"
    )
    github = FakeGitHubClient()
    github.comments.append((GitHubComment("existing-1001"), f"approved body\n\n{marker}"))
    with connection_factory() as connection, connection.transaction():
        run = connection.execute("SELECT id FROM runs WHERE public_id = %s", (RUN_ID,)).fetchone()[
            0
        ]
        connection.execute(
            """
            INSERT INTO external_actions (
                public_id, owner_id, run_id, action_type, target_sha,
                idempotency_key, status
            ) VALUES (%s, %s, %s, 'github.pull_request_comment', %s, %s, 'publishing')
            """,
            (public_id(19), OWNER_ID, run, HEAD_SHA, request.idempotency_key),
        )

    asyncio.run(publisher(connection_factory, github)(request))

    assert github.create_calls == 0
    with connection_factory() as connection:
        assert connection.execute("SELECT status, remote_id FROM external_actions").fetchone() == (
            "published",
            "existing-1001",
        )


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
