"""Read-only structured history of human-approved findings."""

import json
from datetime import UTC

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..reviewer import request_reviewer


def create_findings_export_router(settings, connection_factory, *, sessions=None):
    router = APIRouter()

    @router.get("/api/findings/approved.jsonl", response_class=Response)
    def export_approved_findings(request: Request):
        principal = request_reviewer(request, settings, sessions)
        scope = principal.repository_ids
        # shortcut: buffers one owner's export, stream before supporting large histories.
        with connection_factory() as connection:
            rows = connection.execute(
                """SELECT repo.full_name, pr.github_number, run.head_sha, f.category, f.severity,
                          a.decision, a.decided_at
                   FROM approvals a
                   JOIN findings f ON f.id = a.finding_id AND f.run_id = a.run_id
                     AND f.owner_id = a.owner_id
                   JOIN runs run ON run.id = f.run_id AND run.owner_id = f.owner_id
                     AND run.head_sha = a.head_sha
                   JOIN pull_requests pr ON pr.id = run.pull_request_id
                     AND pr.owner_id = run.owner_id
                   JOIN repositories repo ON repo.id = pr.repository_id
                     AND repo.owner_id = pr.owner_id
                   WHERE a.owner_id = %s AND a.decision = 'approved'
                     AND (%s::bigint[] IS NULL OR repo.id = ANY(%s))
                   ORDER BY a.decided_at, a.id""",
                (principal.owner_id, scope, scope),
            ).fetchall()
        return Response(
            "".join(
                json.dumps(
                    {
                        "repository": repository,
                        "pr_number": number,
                        "head_sha": head,
                        "category": category,
                        "severity": severity,
                        "approval_decision": decision,
                        "approved_at": decided_at.astimezone(UTC).isoformat(),
                        # PR storage does not distinguish merged from closed without merging.
                        "pr_outcome": None,
                    }
                )
                + "\n"
                for repository, number, head, category, severity, decision, decided_at in rows
            ),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'attachment; filename="approved-findings.jsonl"',
            },
        )

    return router
