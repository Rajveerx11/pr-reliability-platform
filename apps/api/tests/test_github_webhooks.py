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


def payload(action: str = "opened", head_sha: str = HEAD_SHA) -> dict[str, object]:
    return {
        "action": action,
        "installation": {"id": 71},
        "repository": {"id": 91, "full_name": "owner/repository"},
        "pull_request": {
            "number": 12,
            "base": {"sha": BASE_SHA},
            "head": {"sha": head_sha},
        },
    }


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


def test_persists_complete_versioned_start_run_command(
    client: TestClient, connection_factory: Callable[[], Connection[object]]
) -> None:
    response = post(client, payload(), delivery="delivery-command")

    with connection_factory() as connection:
        event_data = connection.execute("SELECT event_data FROM run_events").fetchone()[0]
    command = StartRunCommand.model_validate(event_data)
    assert command.public_id == response.json()["command_id"]
    assert command.owner_id == OWNER_ID
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
