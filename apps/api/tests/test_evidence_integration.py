"""PostgreSQL ciphertext, isolation and append-only expiry tombstone evidence."""

from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pr_reliability_api.approvals import ApprovalInboxSettings
from pr_reliability_api.auth.sessions import Principal
from pr_reliability_api.evidence.routes import create_evidence_router
from pr_reliability_evidence import EvidenceSettings
from pr_reliability_evidence.store import evidence_reference, expire, persist
from test_dashboard import (  # Reuse isolated PostgreSQL fixtures, not a live product database.
    ACTOR_ID,
    OTHER_OWNER_ID,
    OWNER_ID,
    TOKEN,
    connection_factory,
    public_id,
    seeded_runs,
)

__all__ = ["connection_factory", "seeded_runs"]


def test_ciphertext_owner_scope_and_expiry_tombstone(connection_factory, seeded_runs):
    config = EvidenceSettings(
        Fernet.generate_key(), retention_seconds=60, secret_patterns=("secret-value",)
    )
    now = datetime.now(UTC)
    references = {}
    with connection_factory() as connection, connection.transaction():
        for index, (key, owner) in enumerate((("awaiting", OWNER_ID), ("hidden", OTHER_OWNER_ID))):
            run = connection.execute(
                "SELECT id FROM runs WHERE owner_id = %s AND public_id = %s",
                (owner, seeded_runs[key]),
            ).fetchone()[0]
            ref = evidence_reference(owner, seeded_runs[key], "unit")
            references[key] = ref
            persist(
                connection,
                config,
                owner_id=owner,
                run_id=run,
                reference=ref,
                check_name="unit",
                payload={"stdout": "secret-value test log", "stderr": "", "status": "passed"},
                expiry_event_id=public_id(800 + index),
                now=now,
            )
        stored = connection.execute("SELECT ciphertext FROM verification_artifacts").fetchall()
        assert all(b"secret-value" not in bytes(row[0]) for row in stored)
        assert all(b"test log" not in bytes(row[0]) for row in stored)

    app = FastAPI()
    app.include_router(
        create_evidence_router(
            ApprovalInboxSettings(OWNER_ID, ACTOR_ID, TOKEN), connection_factory, config
        )
    )
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get(f"/api/evidence/{references['hidden']}", headers=auth).status_code == 404
    response = client.get(f"/api/evidence/{references['awaiting']}", headers=auth)
    assert response.status_code == 200
    assert response.json()["stdout"] == "[redacted] test log"
    assert (
        client.get(f"/api/evidence/runs/{seeded_runs['hidden']}", headers=auth).status_code == 404
    )

    with connection_factory() as connection, connection.transaction():
        assert expire(connection, owner_id=OWNER_ID, now=now + timedelta(seconds=61)) == 1
        assert expire(connection, owner_id=OWNER_ID, now=now + timedelta(seconds=62)) == 0
        row = connection.execute(
            "SELECT ciphertext, expired_at FROM verification_artifacts WHERE owner_id = %s",
            (OWNER_ID,),
        ).fetchone()
        assert row[0] is None and row[1] is not None
        assert connection.execute(
            "SELECT ciphertext IS NOT NULL FROM verification_artifacts WHERE owner_id = %s",
            (OTHER_OWNER_ID,),
        ).fetchone()[0]
        events = connection.execute(
            "SELECT event_data FROM run_events WHERE event_type = 'evidence.expired'"
        ).fetchall()
        assert len(events) == 1
        assert events[0][0] == {
            "evidence_ref": references["awaiting"],
            "reason_code": "retention_expired",
        }
    assert client.get(f"/api/evidence/{references['awaiting']}", headers=auth).status_code == 410

    class Restricted:
        def authorize(self, request, **kwargs):
            return Principal(OWNER_ID, ACTOR_ID, 7, "restricted", "reviewer", [])

    restricted = FastAPI()
    restricted.include_router(
        create_evidence_router(
            ApprovalInboxSettings(OWNER_ID, ACTOR_ID, TOKEN),
            connection_factory,
            config,
            sessions=Restricted(),
        )
    )
    assert TestClient(restricted).get(f"/api/evidence/{references['awaiting']}").status_code == 404
    assert (
        TestClient(restricted).get(f"/api/evidence/runs/{seeded_runs['awaiting']}").status_code
        == 404
    )
