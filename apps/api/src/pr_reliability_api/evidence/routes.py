"""Authenticated owner- and live repository-scoped evidence reads."""

import json
from datetime import UTC, datetime
from typing import Annotated

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import Response
from pr_reliability_evidence import EvidenceSettings
from pr_reliability_evidence.store import expire

from ..reviewer import request_reviewer

_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'",
}
_ULID = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_REF = r"^ev_[0-9a-f]{64}$"


def create_evidence_router(
    settings, connection_factory, evidence: EvidenceSettings, *, sessions=None
):
    router = APIRouter()

    @router.get("/api/evidence/runs/{run_id}")
    def list_evidence(request: Request, run_id: Annotated[str, Path(pattern=_ULID)]):
        principal = request_reviewer(request, settings, sessions)
        scope = principal.repository_ids
        with connection_factory() as connection, connection.transaction():
            expire(connection, owner_id=principal.owner_id)
            run = connection.execute(
                """SELECT r.id FROM runs r JOIN pull_requests pr
                   ON pr.id = r.pull_request_id AND pr.owner_id = r.owner_id
                   WHERE r.owner_id = %s AND r.public_id = %s
                     AND (%s::bigint[] IS NULL OR pr.repository_id = ANY(%s))""",
                (principal.owner_id, run_id, scope, scope),
            ).fetchone()
            if run is None:
                raise HTTPException(404, "Run not found")
            rows = connection.execute(
                """SELECT reference, check_name, expires_at, expired_at
                   FROM verification_artifacts WHERE owner_id = %s AND run_id = %s
                   ORDER BY check_name""",
                (principal.owner_id, run[0]),
            ).fetchall()
        now = datetime.now(UTC)
        return Response(
            json.dumps(
                {
                    "items": [
                        {
                            "reference": ref,
                            "check_name": name,
                            "expires_at": expiry.isoformat(),
                            "expired": expired is not None or expiry <= now,
                        }
                        for ref, name, expiry, expired in rows
                    ]
                }
            ),
            media_type="application/json",
            headers=_HEADERS,
        )

    @router.get("/api/evidence/{reference}")
    def display(
        request: Request, reference: Annotated[str, Path(pattern=_REF)], download: bool = False
    ):
        principal = request_reviewer(request, settings, sessions)
        scope = principal.repository_ids
        with connection_factory() as connection, connection.transaction():
            expire(connection, owner_id=principal.owner_id)
            row = connection.execute(
                """SELECT a.ciphertext, a.expires_at, a.expired_at
                   FROM verification_artifacts a
                   JOIN runs r ON r.id = a.run_id AND r.owner_id = a.owner_id
                   JOIN pull_requests pr ON pr.id = r.pull_request_id AND pr.owner_id = r.owner_id
                   WHERE a.owner_id = %s AND a.reference = %s
                     AND (%s::bigint[] IS NULL OR pr.repository_id = ANY(%s))""",
                (principal.owner_id, reference, scope, scope),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Evidence not found")
        ciphertext, expiry, expired = row
        if expired is not None or expiry <= datetime.now(UTC) or ciphertext is None:
            raise HTTPException(410, "Evidence expired")
        try:
            payload = evidence.decrypt(bytes(ciphertext))
        except (InvalidToken, ValueError, KeyError, TypeError, UnicodeError):
            raise HTTPException(503, "Evidence unavailable") from None
        headers = dict(_HEADERS)
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{reference}.json"'
        return Response(json.dumps(payload), media_type="application/json", headers=headers)

    return router
