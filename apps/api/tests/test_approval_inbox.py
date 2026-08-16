"""Integration tests for authenticated, commit-bound approval decisions."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pr_reliability_api.approvals import ApprovalInboxSettings, create_approval_inbox_router
from pr_reliability_api.db import apply_migrations
from psycopg import Connection

OWNER_ID = "01J00000000000000000000001"
ACTOR_ID = "01J00000000000000000000002"
TOKEN = "test-reviewer-token"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


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


@pytest.fixture
def client(connection_factory: Callable[[], Connection[object]]) -> TestClient:
    ids = iter(public_id(value) for value in range(20, 100))
    app = FastAPI()
    app.include_router(
        create_approval_inbox_router(
            ApprovalInboxSettings(OWNER_ID, ACTOR_ID, TOKEN),
            connection_factory,
            id_factory=lambda: next(ids),
            now=lambda: datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
        )
    )
    return TestClient(app)


@pytest.fixture
def finding_id(connection_factory: Callable[[], Connection[object]]) -> str:
    with connection_factory() as connection, connection.transaction():
        repository_id = connection.execute(
            """
            INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
            VALUES (%s, %s, 91, 'owner/repository') RETURNING id
            """,
            (public_id(1), OWNER_ID),
        ).fetchone()[0]
        pull_request_id = connection.execute(
            """
            INSERT INTO pull_requests (
                public_id, owner_id, repository_id, github_number, base_sha, head_sha
            ) VALUES (%s, %s, %s, 17, %s, %s) RETURNING id
            """,
            (public_id(2), OWNER_ID, repository_id, BASE_SHA, HEAD_SHA),
        ).fetchone()[0]
        run_id = connection.execute(
            """
            INSERT INTO runs (
                public_id, owner_id, pull_request_id, base_sha, head_sha, state,
                token_budget, cost_budget_usd_micros, generation
            ) VALUES (%s, %s, %s, %s, %s, 'awaiting_approval', 100000, 750000, 1)
            RETURNING id
            """,
            (public_id(3), OWNER_ID, pull_request_id, BASE_SHA, HEAD_SHA),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO findings (
                public_id, owner_id, run_id, finding_key, category, severity,
                claim, confidence, evidence
            ) VALUES (%s, %s, %s, 'finding-1', 'correctness', 'high',
                      'Null input crashes the request', 0.95, %s::jsonb)
            """,
            (
                public_id(4),
                OWNER_ID,
                run_id,
                """[{"schema_version":"1","kind":"source_location","summary":"Unsafe access","file_path":"apps/api/example.py","start_line":12},{"schema_version":"1","kind":"test_result","summary":"Regression test passes","command":["pytest","test_example.py"],"exit_code":0}]""",
            ),
        )
    return public_id(4)


def authorization() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def decision(head_sha: str = HEAD_SHA, value: str = "approved") -> dict[str, object]:
    return {
        "schema_version": "1",
        "head_sha": head_sha,
        "decision": value,
        "reason": "Evidence is sufficient",
    }


def test_browser_shell_contains_required_review_fields(client: TestClient) -> None:
    response = client.get("/approval-inbox")

    assert response.status_code == 200, response.text
    assert "Commit" in response.text
    assert "Reported cost" in response.text
    assert "Decisions never publish from this page" in response.text


def test_inbox_requires_authorization_and_shows_full_finding(
    client: TestClient, finding_id: str
) -> None:
    unauthorized = client.get("/api/approval-inbox")
    response = client.get("/api/approval-inbox", headers=authorization())

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert response.status_code == 200
    assert response.json() == [
        {
            "schema_version": "1",
            "finding_id": finding_id,
            "run_id": public_id(3),
            "repository_full_name": "owner/repository",
            "pull_request_number": 17,
            "head_sha": HEAD_SHA,
            "claim": "Null input crashes the request",
            "evidence": [
                {
                    "schema_version": "1",
                    "kind": "source_location",
                    "summary": "Unsafe access",
                    "file_path": "apps/api/example.py",
                    "start_line": 12,
                    "end_line": None,
                    "command": None,
                    "exit_code": None,
                },
                {
                    "schema_version": "1",
                    "kind": "test_result",
                    "summary": "Regression test passes",
                    "file_path": None,
                    "start_line": None,
                    "end_line": None,
                    "command": ["pytest", "test_example.py"],
                    "exit_code": 0,
                },
            ],
            "verification": "passed",
            "cost_usd_micros": None,
            "cost_budget_usd_micros": 750000,
            "status": "pending",
        }
    ]


@pytest.mark.parametrize("value", ["approved", "rejected"])
def test_decision_is_bound_to_finding_and_head_without_external_write(
    client: TestClient,
    connection_factory: Callable[[], Connection[object]],
    finding_id: str,
    value: str,
) -> None:
    response = client.post(
        f"/api/approval-inbox/{finding_id}/decision",
        headers=authorization(),
        json=decision(value=value),
    )

    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["finding_id"] == finding_id
    assert receipt["run_id"] == public_id(3)
    assert receipt["head_sha"] == HEAD_SHA
    assert receipt["decision"] == value
    assert receipt["already_recorded"] is False
    with connection_factory() as connection:
        approval = connection.execute(
            "SELECT actor_id, decision, reason, head_sha FROM approvals"
        ).fetchone()
        events = connection.execute(
            "SELECT event_type, event_data FROM run_events ORDER BY id"
        ).fetchall()
        external_action_count = connection.execute(
            "SELECT count(*) FROM external_actions"
        ).fetchone()[0]
    assert approval == (ACTOR_ID, value, "Evidence is sufficient", HEAD_SHA)
    assert events == [
        (
            "approval.decision_recorded",
            {
                "approval_id": receipt["approval_id"],
                "decision": value,
                "finding_id": finding_id,
                "head_sha": HEAD_SHA,
            },
        )
    ]
    assert external_action_count == 0


def test_same_decision_retries_idempotently_but_conflict_is_rejected(
    client: TestClient, finding_id: str
) -> None:
    first = client.post(
        f"/api/approval-inbox/{finding_id}/decision",
        headers=authorization(),
        json=decision(),
    )
    retry = client.post(
        f"/api/approval-inbox/{finding_id}/decision",
        headers=authorization(),
        json=decision(),
    )
    conflict = client.post(
        f"/api/approval-inbox/{finding_id}/decision",
        headers=authorization(),
        json=decision(value="rejected"),
    )

    assert first.status_code == 200, first.text
    assert retry.status_code == 200
    assert retry.json()["approval_id"] == first.json()["approval_id"]
    assert retry.json()["already_recorded"] is True
    assert conflict.status_code == 409


def test_stale_commit_cannot_be_decided(client: TestClient, finding_id: str) -> None:
    response = client.post(
        f"/api/approval-inbox/{finding_id}/decision",
        headers=authorization(),
        json=decision(head_sha="c" * 40),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "finding commit is stale"
