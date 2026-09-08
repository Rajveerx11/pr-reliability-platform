"""Real PostgreSQL and a deterministic GitHub boundary for browser authorization."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pr_reliability_api.app import create_app
from pr_reliability_api.approvals import ApprovalInboxSettings
from pr_reliability_api.auth.github import Access, Token
from pr_reliability_api.auth.sessions import Sessions
from pr_reliability_api.auth.settings import LoginSettings
from pr_reliability_api.db import apply_migrations
from pr_reliability_api.webhooks import GithubWebhookSettings

OWNER = "01J00000000000000000000001"
OTHER_OWNER = "01J00000000000000000000002"
HEAD = "b" * 40


def public_id(number):
    return f"01J{number:023d}"


@pytest.fixture
def database_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("CI must provide TEST_DATABASE_URL")
        pytest.skip("TEST_DATABASE_URL is required")
    schema = "test_login_" + uuid4().hex
    with psycopg.connect(url) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        connection.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        apply_migrations(connection)

    def factory():
        connection = psycopg.connect(url)
        connection.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    yield factory
    with psycopg.connect(url) as connection:
        connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


class Provider:
    user_id = 11
    repositories = frozenset({91})
    denied = False
    unavailable = False
    calls = 0
    exchanges = 0

    def exchange(self, code, verifier):
        assert code == "valid-code"
        assert len(verifier) >= 43
        self.exchanges += 1
        return Token("test-provider-token")

    def access(self, token):
        assert token == "test-provider-token"
        self.calls += 1
        if self.denied:
            raise HTTPException(403, "GitHub access denied")
        if self.unavailable:
            raise HTTPException(503, "GitHub identity service unavailable")
        return Access(
            self.user_id, "reviewer" if self.user_id == 11 else "admin", self.repositories
        )


def seed(factory, owner, number, github_id):
    with factory() as connection:
        connection.execute(
            """INSERT INTO github_installations (owner_id, installation_id, state, last_sync_at)
               VALUES (%s, 7, 'active', now()) ON CONFLICT DO NOTHING""",
            (owner,),
        )
        repository = connection.execute(
            """INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name,
                    installation_id, access_state) VALUES (%s, %s, %s, %s, 7, 'active') RETURNING id""",
            (public_id(number), owner, github_id, f"owner/repo-{github_id}"),
        ).fetchone()[0]
        pull_request = connection.execute(
            """INSERT INTO pull_requests
               (public_id, owner_id, repository_id, github_number, base_sha, head_sha)
               VALUES (%s, %s, %s, 1, %s, %s) RETURNING id""",
            (public_id(number + 1), owner, repository, "a" * 40, HEAD),
        ).fetchone()[0]
        run = connection.execute(
            """INSERT INTO runs (public_id, owner_id, pull_request_id, base_sha, head_sha,
                    state, token_budget, cost_budget_usd_micros, generation)
               VALUES (%s, %s, %s, %s, %s, 'awaiting_approval', 1000, 1000, 1) RETURNING id""",
            (public_id(number + 2), owner, pull_request, "a" * 40, HEAD),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO findings (public_id, owner_id, run_id, finding_key, category,
                    severity, claim, confidence, evidence)
               VALUES (%s, %s, %s, 'one', 'correctness', 'high', 'A real finding', 0.9,
                 '[{"schema_version":"1","kind":"source_location","summary":"Unsafe access","file_path":"app.py","start_line":1}]')""",
            (public_id(number + 3), owner, run),
        )
    return SimpleNamespace(
        repository=public_id(number), run=public_id(number + 2), finding=public_id(number + 3)
    )


@pytest.fixture
def environment(database_factory):
    settings = LoginSettings(
        OWNER,
        7,
        71,
        "client-id",
        "test-client-secret",
        Fernet.generate_key().decode(),
        "https://reviews.test",
        frozenset({11}),
        frozenset({12}),
    )
    provider = Provider()
    clock = [datetime.now(UTC)]
    sessions = Sessions(settings, database_factory, provider, now=lambda: clock[0])
    app = create_app(
        GithubWebhookSettings(OWNER, 7, b"test-webhook"),
        database_factory,
        ApprovalInboxSettings(OWNER, OWNER, "legacy-test-token"),
        sessions=sessions,
    )
    client = TestClient(app, base_url=settings.origin)
    yield SimpleNamespace(
        settings=settings,
        sessions=sessions,
        provider=provider,
        app=app,
        client=client,
        database=database_factory,
        clock=clock,
        visible=seed(database_factory, OWNER, 10, 91),
        hidden=seed(database_factory, OWNER, 20, 92),
        other=seed(database_factory, OTHER_OWNER, 30, 91),
    )
    client.close()


def login(environment, client=None):
    client = client or environment.client
    response = client.get("/auth/login", follow_redirects=False)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    callback = client.get(
        "/auth/callback",
        params={"state": query["state"][0], "code": "valid-code"},
        follow_redirects=False,
    )
    assert callback.headers["location"] == "/dashboard", callback.text
    return response, callback, query


def csrf(client):
    response = client.get("/auth/session")
    assert response.status_code == 200, response.text
    return {
        "Origin": str(client.base_url).rstrip("/"),
        "X-CSRF-Token": response.json()["csrf_token"],
    }


def decision():
    return {"schema_version": "1", "head_sha": HEAD, "decision": "approved", "reason": None}
