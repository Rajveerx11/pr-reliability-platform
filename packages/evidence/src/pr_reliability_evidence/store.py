"""Transactional encrypted evidence persistence and auditable expiry."""

import hashlib
import json
from datetime import UTC, datetime

from .payload import EvidenceSettings


def evidence_reference(owner_id: str, run_id: str, check_name: str) -> str:
    digest = hashlib.sha256(f"{owner_id}:{run_id}:{check_name}".encode()).hexdigest()
    return f"ev_{digest}"


def persist(
    connection,
    settings: EvidenceSettings,
    *,
    owner_id,
    run_id,
    reference,
    check_name,
    payload,
    expiry_event_id,
    now,
):
    ciphertext, size = settings.encrypt(payload)
    connection.execute(
        """INSERT INTO verification_artifacts
           (owner_id, run_id, reference, check_name, ciphertext, plaintext_bytes,
            created_at, expires_at, expiry_event_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            owner_id,
            run_id,
            reference,
            check_name,
            ciphertext,
            size,
            now,
            now + settings.retention,
            expiry_event_id,
        ),
    )


def expire(connection, *, owner_id: str | None = None, now=None, limit=100) -> int:
    """Caller owns transaction; use bounded batches in a scheduled maintenance job."""
    now = now or datetime.now(UTC)
    rows = connection.execute(
        """WITH due AS (
               SELECT id FROM verification_artifacts
               WHERE expired_at IS NULL AND expires_at <= %s
                 AND (%s::text IS NULL OR owner_id = %s)
               ORDER BY expires_at, id LIMIT %s FOR UPDATE SKIP LOCKED
           )
           UPDATE verification_artifacts AS artifact
           SET ciphertext = NULL, expired_at = %s
           FROM due WHERE artifact.id = due.id
           RETURNING artifact.owner_id, artifact.run_id, artifact.reference,
                     artifact.expiry_event_id""",
        (now, owner_id, owner_id, limit, now),
    ).fetchall()
    for owner, run, reference, public_id in rows:
        connection.execute(
            """INSERT INTO run_events
               (public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at)
               VALUES (%s, %s, %s, %s, 'evidence.expired', %s::jsonb, %s)""",
            (
                public_id,
                owner,
                run,
                f"evidence.expired:{reference}",
                json.dumps({"evidence_ref": reference, "reason_code": "retention_expired"}),
                now,
            ),
        )
    return len(rows)
