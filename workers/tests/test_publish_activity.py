"""Integration tests for approval-bound GitHub review publishing."""

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
    GitHubReview,
    GitHubReviewPayloadMismatch,
    GitHubReviewPublishOperation,
    GitHubReviewStaleHead,
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
SECOND_FINDING_ID = "01J00000000000000000000008"
SECOND_APPROVAL_ID = "01J00000000000000000000009"
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


class FakeGitHubReviewClient:
    def __init__(self, *, current_head: str = HEAD_SHA) -> None:
        self.current_head = current_head
        self.reviews: list[tuple[GitHubReview, str]] = []
        self.create_calls = 0
        self.find_calls = 0

    async def current_head_sha(self, repository: str, pull_request_number: int) -> str:
        assert repository == "owner/repository"
        assert pull_request_number == 17
        return self.current_head

    async def find_review(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        marker: str,
        expected_body: str,
    ) -> GitHubReview | None:
        assert repository == "owner/repository"
        assert pull_request_number == 17
        self.find_calls += 1
        for review, body in self.reviews:
            if body == expected_body and review.commit_sha == expected_head_sha:
                return review
        if any(body.endswith(f"\n\n{marker}") for _, body in self.reviews):
            raise GitHubReviewPayloadMismatch
        return None

    async def create_review(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        body: str,
    ) -> GitHubReview:
        assert repository == "owner/repository"
        assert pull_request_number == 17
        self.create_calls += 1
        review = GitHubReview(str(1000 + self.create_calls), expected_head_sha)
        self.reviews.append((review, body))
        return review


class BlockingGitHubReviewClient(FakeGitHubReviewClient):
    def __init__(self, create_barrier: asyncio.Barrier, release_create: asyncio.Event) -> None:
        super().__init__()
        self.create_barrier = create_barrier
        self.release_create = release_create

    async def create_review(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        body: str,
    ) -> GitHubReview:
        await self.create_barrier.wait()
        await self.release_create.wait()
        return await super().create_review(repository, pull_request_number, expected_head_sha, body)


class LeakyGitHubReviewClient(FakeGitHubReviewClient):
    async def find_review(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        marker: str,
        expected_body: str,
    ) -> GitHubReview | None:
        del repository, pull_request_number, expected_head_sha, marker, expected_body
        raise RuntimeError("SECRET_GITHUB_RESPONSE_BODY")


class HeadAdvancesDuringCreateClient(FakeGitHubReviewClient):
    async def create_review(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        body: str,
    ) -> GitHubReview:
        del repository, pull_request_number, expected_head_sha, body
        self.create_calls += 1
        raise GitHubReviewStaleHead


class SimulatedWorkerCrash(BaseException):
    """Stop the activity after GitHub accepts a review but before its receipt."""


class CrashAfterCreateGitHubReviewClient(FakeGitHubReviewClient):
    def __init__(self, *, current_head: str = HEAD_SHA) -> None:
        super().__init__(current_head=current_head)
        self.crashed = False

    async def create_review(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        body: str,
    ) -> GitHubReview:
        review = await super().create_review(
            repository, pull_request_number, expected_head_sha, body
        )
        if not self.crashed:
            self.crashed = True
            raise SimulatedWorkerCrash
        return review


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


def seed_second_approved_finding(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    with connection_factory() as connection, connection.transaction():
        run_id = connection.execute(
            "SELECT id FROM runs WHERE public_id = %s",
            (RUN_ID,),
        ).fetchone()[0]
        finding_id = connection.execute(
            """
            INSERT INTO findings (
                public_id, owner_id, run_id, finding_key, category, severity,
                claim, confidence, evidence
            ) VALUES (%s, %s, %s, 'finding-2', 'security', 'medium',
                      'Unsafe fallback exposes data', 0.9,
                      '[{"schema_version":"1","kind":"source_location",'
                      '"summary":"Unsafe fallback","file_path":"apps/api/fallback.py"}]'::jsonb)
            RETURNING id
            """,
            (SECOND_FINDING_ID, OWNER_ID, run_id),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO approvals (
                public_id, owner_id, run_id, finding_id, actor_id,
                decision, reason, head_sha, decided_at
            ) VALUES (%s, %s, %s, %s, %s, 'approved', 'reviewed', %s, %s)
            """,
            (
                SECOND_APPROVAL_ID,
                OWNER_ID,
                run_id,
                finding_id,
                ACTOR_ID,
                HEAD_SHA,
                datetime(2026, 8, 16, 8, 1, tzinfo=UTC),
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
    client: FakeGitHubReviewClient,
) -> GitHubReviewPublishOperation:
    ids = iter(public_id(value) for value in range(20, 100))
    return GitHubReviewPublishOperation(
        connection_factory,
        client,
        id_factory=lambda: next(ids),
        now=lambda: datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
    )


def test_unapproved_finding_cannot_publish(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory, decision=None)
    github = FakeGitHubReviewClient()

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(publish_request()))

    assert raised.value.type == "PublishBlocked"
    assert raised.value.non_retryable
    assert github.reviews == []
    with connection_factory() as connection:
        action_count = connection.execute("SELECT count(*) FROM external_actions").fetchone()[0]
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action_count == 0
    assert event == ("github.review_publish_blocked", {"reason_code": "approval_missing"})


def test_empty_approval_set_cannot_publish_and_records_safe_block(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = FakeGitHubReviewClient()
    request = replace(
        publish_request(),
        finding_ids=(),
        approval_ids=(),
        comment_body_ref="SECRET_REVIEW_BODY",
    )

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(request))

    assert raised.value.type == "PublishBlocked"
    assert raised.value.non_retryable
    assert github.find_calls == 0
    assert github.create_calls == 0
    assert github.reviews == []
    with connection_factory() as connection:
        action_count = connection.execute("SELECT count(*) FROM external_actions").fetchone()[0]
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action_count == 0
    assert event == (
        "github.review_publish_blocked",
        {"reason_code": "empty_approval_set"},
    )
    assert "SECRET_REVIEW_BODY" not in str(event)


def test_rejected_approval_cannot_publish_and_records_safe_block(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory, decision="rejected")
    github = FakeGitHubReviewClient()

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(publish_request()))

    assert raised.value.type == "PublishBlocked"
    assert github.reviews == []
    with connection_factory() as connection:
        action_count = connection.execute("SELECT count(*) FROM external_actions").fetchone()[0]
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action_count == 0
    assert event == ("github.review_publish_blocked", {"reason_code": "approval_rejected"})


def test_wrong_run_state_cannot_publish_and_records_safe_block(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    with connection_factory() as connection, connection.transaction():
        connection.execute(
            "UPDATE runs SET state = 'failed' WHERE public_id = %s",
            (RUN_ID,),
        )
    github = FakeGitHubReviewClient()

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(publish_request()))

    assert raised.value.type == "PublishBlocked"
    assert github.reviews == []
    with connection_factory() as connection:
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert event == (
        "github.review_publish_blocked",
        {"reason_code": "run_not_awaiting_approval"},
    )


def test_stable_retry_creates_one_review_and_safe_audit(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = FakeGitHubReviewClient()
    operation = publisher(connection_factory, github)

    asyncio.run(operation(publish_request()))
    asyncio.run(operation(publish_request()))

    assert github.create_calls == 1
    assert len(github.reviews) == 1
    assert "Null input crashes the request" in github.reviews[0][1]
    with connection_factory() as connection:
        action = connection.execute(
            "SELECT status, remote_id, payload_fingerprint FROM external_actions"
        ).fetchone()
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action[:2] == ("published", "1001")
    assert len(action[2]) == 64
    assert event[0] == "github.review_published"
    assert event[1] == {
        "action_id": public_id(20),
        "remote_review_id": "1001",
        "head_sha": HEAD_SHA,
        "finding_ids": [FINDING_ID],
        "approval_ids": [APPROVAL_ID],
        "payload_fingerprint": action[2],
    }
    assert "Null input" not in str(event[1])


def test_two_approved_findings_retry_to_one_review_summary(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    seed_second_approved_finding(connection_factory)
    request = replace(
        publish_request(),
        finding_ids=(FINDING_ID, SECOND_FINDING_ID),
        approval_ids=(APPROVAL_ID, SECOND_APPROVAL_ID),
        comment_body_ref="approved-finding-set",
    )
    github = FakeGitHubReviewClient()
    operation = publisher(connection_factory, github)

    asyncio.run(operation(request))
    asyncio.run(operation(request))

    assert github.create_calls == 1
    assert len(github.reviews) == 1
    body = github.reviews[0][1]
    assert "Null input crashes the request" in body
    assert "Unsafe fallback exposes data" in body
    with connection_factory() as connection:
        event = connection.execute(
            "SELECT event_data FROM run_events WHERE event_type = 'github.review_published'"
        ).fetchone()[0]
    assert event["finding_ids"] == [FINDING_ID, SECOND_FINDING_ID]
    assert event["approval_ids"] == [APPROVAL_ID, SECOND_APPROVAL_ID]


def test_concurrent_retries_hold_one_publish_claim(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)

    async def scenario() -> BlockingGitHubReviewClient:
        create_barrier = asyncio.Barrier(2)
        release_create = asyncio.Event()
        github = BlockingGitHubReviewClient(create_barrier, release_create)
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
    assert len(github.reviews) == 1
    with connection_factory() as connection:
        assert connection.execute("SELECT status, remote_id FROM external_actions").fetchone() == (
            "published",
            "1001",
        )
        assert connection.execute("SELECT count(*) FROM run_events").fetchone()[0] == 1


def test_retry_recovers_review_created_before_database_receipt(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    request = publish_request()
    github = CrashAfterCreateGitHubReviewClient()
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


def test_retry_reconciles_created_review_after_head_advances(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    request = publish_request()
    github = CrashAfterCreateGitHubReviewClient()
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
            "github.review_published"
        )


def test_published_retry_with_different_payload_fails_closed(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = FakeGitHubReviewClient()
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
    github = CrashAfterCreateGitHubReviewClient()
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


def test_crash_recovery_rejects_edited_review_with_same_terminal_marker(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = CrashAfterCreateGitHubReviewClient()
    operation = publisher(connection_factory, github)
    request = publish_request()

    with pytest.raises(SimulatedWorkerCrash):
        asyncio.run(operation(request))
    review, original_body = github.reviews[0]
    marker = original_body.rsplit("\n\n", 1)[1]
    github.reviews[0] = (review, f"edited after publication\n\n{marker}")

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(operation(request))

    assert raised.value.type == "PublishBlocked"
    assert raised.value.non_retryable
    assert github.create_calls == 1
    with connection_factory() as connection:
        action = connection.execute("SELECT status, remote_id FROM external_actions").fetchone()
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action == ("failed", None)
    assert event == ("github.review_publish_blocked", {"reason_code": "review_payload_mismatch"})


def test_provider_failure_is_sanitized_before_temporal_conversion(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(publisher(connection_factory, LeakyGitHubReviewClient())(publish_request()))

    assert str(raised.value) == "GitHub review publish failed"
    assert raised.value.__cause__ is None
    failure = Failure()
    DefaultFailureConverter.default.to_failure(
        raised.value,
        DefaultPayloadConverter.default,
        failure,
    )
    assert "SECRET_GITHUB_RESPONSE_BODY" not in str(failure)
    with connection_factory() as connection:
        action = connection.execute("SELECT status FROM external_actions").fetchone()[0]
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action == "failed"
    assert event == (
        "github.review_publish_failed",
        {
            "action_id": public_id(20),
            "head_sha": HEAD_SHA,
            "failure_code": "github_error",
        },
    )
    assert "SECRET_GITHUB_RESPONSE_BODY" not in str(event)


def test_database_stale_head_blocks_before_github(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory, current_head=NEXT_HEAD_SHA)
    github = FakeGitHubReviewClient()

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(publish_request()))

    assert raised.value.type == "PublishBlocked"
    with connection_factory() as connection:
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert event == ("github.review_publish_blocked", {"reason_code": "stale_head"})
    assert github.reviews == []


def test_remote_stale_head_blocks_and_records_safe_failure(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = FakeGitHubReviewClient(current_head=NEXT_HEAD_SHA)

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(publish_request()))

    assert raised.value.type == "PublishBlocked"
    assert github.reviews == []
    with connection_factory() as connection:
        action = connection.execute("SELECT status FROM external_actions").fetchone()[0]
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action == "failed"
    assert event == ("github.review_publish_blocked", {"reason_code": "stale_head"})


def test_head_change_during_pending_review_blocks_and_records_safe_failure(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_review(connection_factory)
    github = HeadAdvancesDuringCreateClient()

    with pytest.raises(ApplicationError) as raised:
        asyncio.run(publisher(connection_factory, github)(publish_request()))

    assert raised.value.type == "PublishBlocked"
    assert raised.value.non_retryable
    assert github.create_calls == 1
    with connection_factory() as connection:
        action = connection.execute("SELECT status FROM external_actions").fetchone()[0]
        event = connection.execute("SELECT event_type, event_data FROM run_events").fetchone()
    assert action == "failed"
    assert event == ("github.review_publish_blocked", {"reason_code": "stale_head"})
