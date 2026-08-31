"""Integration tests for the authenticated, owner-scoped operations dashboard."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pr_reliability_api.approvals import ApprovalInboxSettings
from pr_reliability_api.dashboard import create_dashboard_router
from pr_reliability_api.db import apply_migrations
from psycopg import Connection

OWNER_ID = "01J00000000000000000000001"
OTHER_OWNER_ID = "01J00000000000000000000002"
ACTOR_ID = "01J00000000000000000000003"
TOKEN = "test-reviewer-token"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
TRACE_ID = "1" * 32


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
    app = FastAPI()
    app.include_router(
        create_dashboard_router(
            ApprovalInboxSettings(OWNER_ID, ACTOR_ID, TOKEN),
            connection_factory,
        )
    )
    return TestClient(app)


@pytest.fixture
def seeded_runs(connection_factory: Callable[[], Connection[object]]) -> dict[str, str]:
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    evidence = (
        '[{"schema_version":"1","kind":"source_location",'
        '"summary":"Unsafe access","file_path":"apps/api/example.py",'
        '"start_line":12}]'
    )
    with connection_factory() as connection, connection.transaction():
        repository_id = _insert_repository(connection, OWNER_ID, 91, "owner/repository", 1)
        pull_request_id = _insert_pull_request(connection, OWNER_ID, repository_id, 17, 2)
        awaiting = _insert_run(
            connection,
            OWNER_ID,
            pull_request_id,
            "awaiting_approval",
            3,
            now,
            now + timedelta(seconds=10),
        )
        published = _insert_run(
            connection,
            OWNER_ID,
            pull_request_id,
            "published",
            4,
            now - timedelta(minutes=2),
            now,
            head_sha="c" * 40,
            generation=2,
        )
        connection.execute(
            """
            INSERT INTO findings (
                public_id, owner_id, run_id, finding_key, category, severity,
                claim, confidence, evidence
            ) VALUES (%s, %s, %s, 'finding-1', 'correctness', 'high',
                      'Null input crashes the request', 0.95, %s::jsonb)
            """,
            (public_id(5), OWNER_ID, awaiting[1], evidence),
        )
        connection.execute(
            """
            INSERT INTO run_events (
                public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
            ) VALUES (%s, %s, %s, 'queued', 'run.command_created',
                      %s::jsonb, %s)
            """,
            (
                public_id(6),
                OWNER_ID,
                awaiting[1],
                '{"traceparent":"00-' + TRACE_ID + "-" + "2" * 16 + '-01"}',
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO run_events (
                public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
            ) VALUES
                (%s, %s, %s, 'dispatch', 'run.command_dispatched',
                 '{"status":"accepted","workflow_id":"safe-id"}'::jsonb, %s),
                (%s, %s, %s, 'approval-skipped', 'approval.signal_dispatched',
                 '{"status":"skipped","reason":"run no longer awaits this commit"}'::jsonb,
                 %s)
            """,
            (
                public_id(7),
                OWNER_ID,
                awaiting[1],
                now,
                public_id(8),
                OWNER_ID,
                awaiting[1],
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO findings (
                public_id, owner_id, run_id, finding_key, category, severity,
                claim, confidence, evidence
            ) VALUES (%s, %s, %s, 'historic-finding', 'correctness', 'medium',
                      'Historic finding', 0.75, %s::jsonb)
            """,
            (public_id(9), OWNER_ID, published[1], evidence),
        )

        hidden_repository = _insert_repository(connection, OTHER_OWNER_ID, 92, "other/private", 20)
        hidden_pull_request = _insert_pull_request(
            connection, OTHER_OWNER_ID, hidden_repository, 18, 21
        )
        hidden = _insert_run(
            connection,
            OTHER_OWNER_ID,
            hidden_pull_request,
            "failed",
            22,
            now,
            now + timedelta(seconds=1),
        )
    return {"awaiting": awaiting[0], "published": published[0], "hidden": hidden[0]}


def _insert_repository(
    connection: Connection[object], owner_id: str, github_id: int, name: str, sequence: int
) -> int:
    return connection.execute(
        """
        INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (public_id(sequence), owner_id, github_id, name),
    ).fetchone()[0]


def _insert_pull_request(
    connection: Connection[object],
    owner_id: str,
    repository_id: int,
    number: int,
    sequence: int,
) -> int:
    return connection.execute(
        """
        INSERT INTO pull_requests (
            public_id, owner_id, repository_id, github_number, base_sha, head_sha
        ) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (public_id(sequence), owner_id, repository_id, number, BASE_SHA, HEAD_SHA),
    ).fetchone()[0]


def _insert_run(
    connection: Connection[object],
    owner_id: str,
    pull_request_id: int,
    state: str,
    sequence: int,
    created_at: datetime,
    updated_at: datetime,
    *,
    head_sha: str = HEAD_SHA,
    generation: int = 1,
) -> tuple[str, int]:
    run_public_id = public_id(sequence)
    internal_id = connection.execute(
        """
        INSERT INTO runs (
            public_id, owner_id, pull_request_id, base_sha, head_sha, state,
            token_budget, cost_budget_usd_micros, generation, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, 100000, 750000, %s, %s, %s)
        RETURNING id
        """,
        (
            run_public_id,
            owner_id,
            pull_request_id,
            BASE_SHA,
            head_sha,
            state,
            generation,
            created_at,
            updated_at,
        ),
    ).fetchone()[0]
    return run_public_id, internal_id


def authorization(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_dashboard_shell_is_local_and_hardened(client: TestClient) -> None:
    page = client.get("/dashboard")
    script = client.get("/dashboard/assets/dashboard.js")

    assert page.status_code == 200
    assert script.status_code == 200
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"
    assert "Reviewer token" in page.text
    assert "localStorage" not in script.text


def test_overview_requires_exact_bearer_and_is_owner_scoped(
    client: TestClient, seeded_runs: dict[str, str]
) -> None:
    missing = client.get("/api/dashboard/overview")
    wrong = client.get("/api/dashboard/overview", headers=authorization("wrong"))
    response = client.get("/api/dashboard/overview", headers=authorization())

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["total_runs"] == 2
    assert body["active_runs"] == 1
    assert body["awaiting_approval_runs"] == 1
    assert body["pending_findings"] == 1
    assert body["published_runs"] == 1
    assert body["failed_runs"] == 0
    assert body["p50_duration_ms"] == 120000
    assert body["p95_duration_ms"] == 120000
    assert body["activity_retry_count"] is None
    assert body["usage_unknown_runs"] == 2
    assert body["exact_known_cost_usd_micros"] is None


def test_run_list_filters_exactly_and_never_leaks_other_owner(
    client: TestClient, seeded_runs: dict[str, str]
) -> None:
    response = client.get("/api/dashboard/runs", headers=authorization())
    filtered = client.get(
        "/api/dashboard/runs?status=published&repository=owner%2Frepository",
        headers=authorization(),
    )
    injection = client.get(
        "/api/dashboard/runs?repository=owner%2Frepository%27%20OR%201%3D1--",
        headers=authorization(),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {item["run_id"] for item in response.json()["items"]} == {
        seeded_runs["awaiting"],
        seeded_runs["published"],
    }
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["state"] == "published"
    assert filtered.json()["items"][0]["finding_count"] == 1
    assert filtered.json()["items"][0]["pending_finding_count"] == 0
    assert filtered.json()["items"][0]["retry_count"] is None
    assert injection.json()["total"] == 0


def test_run_detail_returns_safe_timeline_and_evidence(
    client: TestClient, seeded_runs: dict[str, str]
) -> None:
    response = client.get(f"/api/dashboard/runs/{seeded_runs['awaiting']}", headers=authorization())
    hidden = client.get(f"/api/dashboard/runs/{seeded_runs['hidden']}", headers=authorization())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trace_id"] == TRACE_ID
    assert [event["summary"] for event in body["events"]] == [
        "Run queued from GitHub webhook",
        "Run accepted by workflow",
        "Approval delivery skipped for inactive commit",
    ]
    assert body["stages"] == [
        {"schema_version": "1", "name": "webhook", "status": "completed"},
        {"schema_version": "1", "name": "dispatch", "status": "completed"},
        {"schema_version": "1", "name": "select_context", "status": "completed"},
        {"schema_version": "1", "name": "analyze", "status": "completed"},
        {"schema_version": "1", "name": "verify", "status": "completed"},
        {"schema_version": "1", "name": "approval", "status": "current"},
        {"schema_version": "1", "name": "publish", "status": "not_started"},
    ]
    assert body["findings"][0]["claim"] == "Null input crashes the request"
    assert body["findings"][0]["approval_status"] == "pending"
    assert "event_data" not in response.text
    assert "traceparent" not in response.text
    assert "run no longer awaits this commit" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert hidden.status_code == 404

    historic = client.get(
        f"/api/dashboard/runs/{seeded_runs['published']}", headers=authorization()
    )
    assert historic.json()["findings"][0]["approval_status"] == "not_actionable"


def test_pagination_bounds_are_validated(client: TestClient) -> None:
    assert client.get("/api/dashboard/runs?limit=51", headers=authorization()).status_code == 422
    assert (
        client.get("/api/dashboard/runs?offset=10001", headers=authorization()).status_code == 422
    )
    assert (
        client.get("/api/dashboard/runs/not-a-run-id", headers=authorization()).status_code == 422
    )
