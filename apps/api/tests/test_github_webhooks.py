"""Integration tests for signed, deduplicated GitHub webhook intake."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pr_reliability_api.app import create_app
from pr_reliability_api.db import apply_migrations
from pr_reliability_api.webhooks import GithubWebhookSettings, create_github_webhook_router
from pr_reliability_contracts import StartRunCommand
from psycopg import Connection

OWNER_ID = "01J00000000000000000000001"
SECRET = b"test-only-secret"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def ids() -> Iterator[str]:
    sequence = 10
    while True:
        yield f"01J{sequence:023d}"
        sequence += 1


@pytest.fixture
def database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL")
    if value is None:
        if os.environ.get("CI"):
            pytest.fail("CI must provide TEST_DATABASE_URL")
        pytest.skip("TEST_DATABASE_URL is required")
    return value


@pytest.fixture
def connection_factory(database_url: str) -> Iterator[Callable[[], Connection[object]]]:
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
    id_values = ids()
    app = FastAPI()
    app.include_router(
        create_github_webhook_router(
            GithubWebhookSettings(owner_id=OWNER_ID, installation_id=71, webhook_secret=SECRET),
            connection_factory,
            id_factory=lambda: next(id_values),
            now=lambda: datetime(2026, 8, 13, tzinfo=UTC),
        )
    )
    return TestClient(app)


def test_production_app_factory_registers_webhook(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    app = create_app(
        GithubWebhookSettings(owner_id=OWNER_ID, installation_id=71, webhook_secret=SECRET),
        connection_factory,
    )

    assert "/webhooks/github" in app.openapi()["paths"]


def payload(
    action: str = "opened",
    head_sha: str = HEAD_SHA,
    *,
    updated_at: str = "2026-08-13T08:00:00Z",
    full_name: str = "owner/repository",
    before_sha: str = HEAD_SHA,
) -> dict[str, object]:
    data = {
        "action": action,
        "installation": {"id": 71},
        "repository": {"id": 91, "full_name": full_name},
        "pull_request": {
            "number": 12,
            "updated_at": updated_at,
            "base": {"sha": BASE_SHA},
            "head": {"sha": head_sha},
        },
    }
    if action == "synchronize":
        data["before"] = before_sha
        data["after"] = head_sha
    return data


def headers(body: bytes, *, delivery: str = "delivery-1") -> dict[str, str]:
    signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
    }


def post(client: TestClient, data: dict[str, object], *, delivery: str = "delivery-1"):
    body = json.dumps(data, separators=(",", ":")).encode()
    return client.post("/webhooks/github", content=body, headers=headers(body, delivery=delivery))


def scalar(factory: Callable[[], Connection[object]], sql: str) -> int:
    with factory() as connection:
        return int(connection.execute(sql).fetchone()[0])


def test_invalid_signature_fails_before_payload_use(client: TestClient) -> None:
    response = client.post(
        "/webhooks/github",
        content=b"not-json-and-never-parsed",
        headers={
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "X-GitHub-Delivery": "delivery-invalid",
            "X-GitHub-Event": "pull_request",
        },
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid signature"}


def test_non_ascii_signature_is_rejected_without_server_error(client: TestClient) -> None:
    response = client.post(
        "/webhooks/github",
        content=b"{}",
        headers=[
            (b"X-Hub-Signature-256", b"sha256=\xe9"),
            (b"X-GitHub-Delivery", b"delivery-bad-signature"),
            (b"X-GitHub-Event", b"pull_request"),
        ],
    )

    assert response.status_code == 401


def test_repeated_delivery_creates_one_command(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    first = post(client, payload())
    second = post(client, payload())

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert first.json()["command_id"] is not None
    assert second.json() == {"accepted": True, "duplicate": True, "command_id": None}
    assert scalar(connection_factory, "SELECT count(*) FROM github_webhook_deliveries") == 1
    assert scalar(connection_factory, "SELECT count(*) FROM runs") == 1
    assert scalar(connection_factory, "SELECT count(*) FROM run_events") == 1


def test_different_delivery_for_same_head_creates_no_duplicate_command(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    first = post(client, payload(), delivery="delivery-one")
    second = post(client, payload(), delivery="delivery-two")

    assert first.json()["command_id"] is not None
    assert second.json() == {"accepted": True, "duplicate": False, "command_id": None}
    assert scalar(connection_factory, "SELECT count(*) FROM github_webhook_deliveries") == 2
    assert scalar(connection_factory, "SELECT count(*) FROM runs") == 1
    assert scalar(connection_factory, "SELECT count(*) FROM run_events") == 1


def test_reopen_same_head_creates_new_run_generation(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    opened = post(client, payload(updated_at="2026-08-13T08:00:00Z"), delivery="opened")
    closed = post(
        client,
        payload("closed", updated_at="2026-08-13T08:01:00Z"),
        delivery="closed",
    )
    reopened = post(
        client,
        payload("reopened", updated_at="2026-08-13T08:02:00Z"),
        delivery="reopened",
    )

    assert opened.json()["command_id"] is not None
    assert closed.json()["command_id"] is None
    assert reopened.json()["command_id"] is not None
    assert scalar(connection_factory, "SELECT count(*) FROM runs") == 2
    with connection_factory() as connection:
        generations = connection.execute(
            "SELECT generation FROM runs ORDER BY generation"
        ).fetchall()
    assert generations == [(1,), (2,)]


def test_older_delivery_does_not_regress_pull_request(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    newest_head = "c" * 40
    newest = post(
        client,
        payload("synchronize", newest_head, updated_at="2026-08-13T08:02:00Z"),
        delivery="newest",
    )
    stale = post(
        client,
        payload("closed", HEAD_SHA, updated_at="2026-08-13T08:01:00Z"),
        delivery="stale",
    )

    assert newest.json()["command_id"] is not None
    assert stale.json()["command_id"] is None
    with connection_factory() as connection:
        state = connection.execute("SELECT head_sha, state FROM pull_requests").fetchone()
    assert state == (newest_head, "open")


def test_equal_timestamp_uses_delivery_order_for_state_transition(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    timestamp = "2026-08-13T08:00:00Z"
    post(client, payload("closed", updated_at=timestamp), delivery="delivery-closed")
    reopened = post(
        client,
        payload("reopened", updated_at=timestamp),
        delivery="delivery-reopened",
    )

    assert reopened.json()["command_id"] is not None
    with connection_factory() as connection:
        state = connection.execute("SELECT state FROM pull_requests").fetchone()[0]
    assert state == "open"


def test_equal_timestamp_delayed_close_cannot_suppress_reopened_state(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    timestamp = "2026-08-13T08:00:00Z"
    reopened = post(
        client,
        payload("reopened", updated_at=timestamp),
        delivery="delivery-reopened-first",
    )
    delayed_close = post(
        client,
        payload("closed", updated_at=timestamp),
        delivery="delivery-closed-late",
    )

    assert reopened.json()["command_id"] is not None
    assert delayed_close.json()["command_id"] is None
    with connection_factory() as connection:
        state = connection.execute("SELECT state FROM pull_requests").fetchone()[0]
    assert state == "open"


def test_stale_delivery_does_not_regress_repository_name(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    post(
        client,
        payload(updated_at="2026-08-13T08:02:00Z", full_name="owner/new-name"),
        delivery="new-name",
    )
    post(
        client,
        payload(updated_at="2026-08-13T08:01:00Z", full_name="owner/old-name"),
        delivery="old-name",
    )

    with connection_factory() as connection:
        full_name = connection.execute("SELECT full_name FROM repositories").fetchone()[0]
    assert full_name == "owner/new-name"


def test_equal_timestamp_synchronize_chain_advances_to_latest_head(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    first_head = "c" * 40
    latest_head = "d" * 40
    timestamp = "2026-08-13T08:00:00Z"
    first = post(
        client,
        payload("synchronize", first_head, updated_at=timestamp),
        delivery="sync-first",
    )
    latest = post(
        client,
        payload(
            "synchronize",
            latest_head,
            updated_at=timestamp,
            before_sha=first_head,
        ),
        delivery="sync-latest",
    )

    assert first.json()["command_id"] is not None
    assert latest.json()["command_id"] is not None
    with connection_factory() as connection:
        head_sha = connection.execute("SELECT head_sha FROM pull_requests").fetchone()[0]
        generations = connection.execute(
            "SELECT generation FROM runs ORDER BY generation"
        ).fetchall()
    assert head_sha == latest_head
    assert generations == [(1,), (2,)]


def test_out_of_order_synchronize_chain_does_not_regress_latest_head(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    earlier_head = "c" * 40
    latest_head = "d" * 40
    timestamp = "2026-08-13T08:00:00Z"
    latest = post(
        client,
        payload(
            "synchronize",
            latest_head,
            updated_at=timestamp,
            before_sha=earlier_head,
        ),
        delivery="sync-latest-first",
    )
    delayed = post(
        client,
        payload("synchronize", earlier_head, updated_at=timestamp),
        delivery="sync-earlier-late",
    )

    assert latest.json()["command_id"] is not None
    assert delayed.json()["command_id"] is None
    with connection_factory() as connection:
        head_sha = connection.execute("SELECT head_sha FROM pull_requests").fetchone()[0]
        run_heads = connection.execute("SELECT head_sha FROM runs").fetchall()
        command_heads = connection.execute(
            "SELECT event_data ->> 'head_sha' FROM run_events"
        ).fetchall()
    assert head_sha == latest_head
    assert run_heads == [(latest_head,)]
    assert command_heads == [(latest_head,)]


def test_persists_complete_versioned_start_run_command(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    response = post(client, payload(), delivery="delivery-command")

    with connection_factory() as connection:
        event_data = connection.execute("SELECT event_data FROM run_events").fetchone()[0]
    command = StartRunCommand.model_validate(event_data)
    assert command.public_id == response.json()["command_id"]
    assert command.owner_id == OWNER_ID
    assert command.generation == 1
    assert command.head_sha == HEAD_SHA
    assert command.pull_request_number == 12
    assert command.base_sha == BASE_SHA
    assert command.token_budget == 100_000
    assert command.cost_budget_usd_micros == 1_000_000


@pytest.mark.parametrize("action", ["opened", "reopened", "synchronize"])
def test_supported_run_actions_create_command(client: TestClient, action: str) -> None:
    response = post(client, payload(action), delivery=f"delivery-{action}")

    assert response.status_code == 200
    assert response.json()["command_id"] is not None


def test_closed_action_updates_pr_without_run(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    response = post(client, payload("closed"), delivery="delivery-closed")

    assert response.status_code == 200
    assert response.json()["command_id"] is None
    assert scalar(connection_factory, "SELECT count(*) FROM runs") == 0
    with connection_factory() as connection:
        state = connection.execute("SELECT state FROM pull_requests").fetchone()[0]
    assert state == "closed"


def test_unsupported_action_fails_closed(client: TestClient) -> None:
    response = post(client, payload("edited"), delivery="delivery-edited")

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid payload"}


def test_other_installation_cannot_write_to_owner(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    data = payload()
    data["installation"] = {"id": 999}

    response = post(client, data, delivery="delivery-other-installation")

    assert response.status_code == 403
    assert response.json() == {"detail": "installation is not authorized"}
    assert scalar(connection_factory, "SELECT count(*) FROM github_webhook_deliveries") == 0


def test_raw_payload_and_secret_are_not_logged(client: TestClient, caplog) -> None:
    marker = "PRIVATE_PAYLOAD_MARKER"
    data = {**payload(), "marker": marker}

    assert post(client, data, delivery="delivery-private").status_code == 200
    log_text = caplog.text
    assert marker not in log_text
    assert SECRET.decode() not in log_text
