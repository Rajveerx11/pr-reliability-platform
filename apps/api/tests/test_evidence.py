"""Private evidence API authorization, download, expiry and encryption tests."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pr_reliability_api.approvals import ApprovalInboxSettings
from pr_reliability_api.auth.sessions import Principal
from pr_reliability_api.evidence.routes import create_evidence_router
from pr_reliability_evidence import EvidenceSettings

OWNER = "01J00000000000000000000001"
REF = "ev_" + "a" * 64
RUN = "01J00000000000000000000004"


class Cursor:
    def __init__(self, row=None, rows=()):
        self.row, self.rows = row, rows

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def transaction(self):
        return nullcontext()

    def execute(self, sql, values):
        self.calls.append((sql, values))
        if "WITH due" in sql:
            return Cursor()
        if "SELECT a.ciphertext" in sql:
            return Cursor(self.row)
        if "SELECT r.id" in sql:
            return Cursor((1,))
        return Cursor(rows=[(REF, "unit", datetime.now(UTC) + timedelta(hours=1), None)])


def client_for(row=None, *, sessions=None, config=None):
    config = config or EvidenceSettings(Fernet.generate_key())
    connection = Connection(row)
    app = FastAPI()
    app.include_router(
        create_evidence_router(
            ApprovalInboxSettings(OWNER, OWNER, "test-only-token"),
            lambda: connection,
            config,
            sessions=sessions,
        )
    )
    return TestClient(app), connection, config


def test_unauthenticated_display_download_and_listing_do_not_touch_database():
    client, connection, _ = client_for()
    for path in (
        f"/api/evidence/{REF}",
        f"/api/evidence/{REF}?download=true",
        f"/api/evidence/runs/{RUN}",
    ):
        assert client.get(path).status_code == 401
    assert connection.calls == []


def test_download_decrypts_redacted_json_with_private_headers():
    config = EvidenceSettings(Fernet.generate_key(), secret_patterns=("test-secret",))
    ciphertext, _ = config.encrypt({"stdout": "test-secret <script>", "stderr": ""})
    client, connection, _ = client_for(
        (ciphertext, datetime.now(UTC) + timedelta(hours=1), None),
        config=config,
    )
    response = client.get(
        f"/api/evidence/{REF}?download=true", headers={"Authorization": "Bearer test-only-token"}
    )
    assert response.status_code == 200
    assert response.json()["stdout"] == "[redacted] <script>"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert response.headers["Content-Type"].startswith("application/json")
    assert connection.calls[-1][1] == (OWNER, REF, None, None)


@pytest.mark.parametrize(
    "row,code",
    [
        (None, 404),
        ((None, datetime.now(UTC) - timedelta(seconds=1), datetime.now(UTC)), 410),
        ((b"invalid-ciphertext", datetime.now(UTC) + timedelta(hours=1), None), 503),
    ],
)
def test_unavailable_expired_or_corrupt_fails_without_content(row, code):
    client, _, _ = client_for(row)
    response = client.get(
        f"/api/evidence/{REF}", headers={"Authorization": "Bearer test-only-token"}
    )
    assert response.status_code == code
    assert "ciphertext" not in response.text


def test_repository_scope_applies_to_display_and_listing():
    class Sessions:
        def authorize(self, request, **kwargs):
            return Principal(OWNER, OWNER, 7, "reviewer", "reviewer", [123])

    client, connection, _ = client_for(sessions=Sessions())
    assert client.get(f"/api/evidence/{REF}").status_code == 404
    assert connection.calls[-1][1] == (OWNER, REF, [123], [123])
    assert client.get(f"/api/evidence/runs/{RUN}").status_code == 200
    query = next(call for call in connection.calls if "SELECT r.id" in call[0])
    assert query[1] == (OWNER, RUN, [123], [123])


def test_foreign_owner_session_cannot_use_configured_owner():
    class Sessions:
        def authorize(self, request, **kwargs):
            return Principal("01J00000000000000000000002", OWNER, 8, "foreign", "reviewer", None)

    client, connection, _ = client_for(sessions=Sessions())
    assert client.get(f"/api/evidence/{REF}").status_code == 403
    assert not connection.calls


@pytest.mark.parametrize("reference", ["../file", "C:\\secret", "ev_not-a-reference"])
def test_arbitrary_paths_are_not_accepted(reference):
    client, _, _ = client_for()
    assert client.get(
        f"/api/evidence/{reference}", headers={"Authorization": "Bearer test-only-token"}
    ).status_code in {404, 422}
