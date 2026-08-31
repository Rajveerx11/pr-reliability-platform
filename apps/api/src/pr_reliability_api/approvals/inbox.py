"""List proposed findings and record commit-bound human decisions."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import HTMLResponse
from pr_reliability_contracts import (
    ApprovalCommand,
    ApprovalDecision,
    ApprovalDecisionReceipt,
    ApprovalDecisionRequest,
    ApprovalInboxItem,
    Evidence,
    EvidenceKind,
    FindingApprovalStatus,
    VerificationStatus,
)
from psycopg import Connection

from ..identifiers import new_ulid
from ..reviewer import authorize_reviewer

ConnectionFactory = Callable[[], Connection[Any]]
IdFactory = Callable[[], str]
_PACKAGED_WEB_PAGE = Path(__file__).parents[1] / "_web" / "approval_inbox.html"
_SOURCE_WEB_PAGE = Path(__file__).parents[4] / "web" / "approval_inbox.html"
_WEB_PAGE = _PACKAGED_WEB_PAGE if _PACKAGED_WEB_PAGE.exists() else _SOURCE_WEB_PAGE


@dataclass(frozen=True, slots=True)
class ApprovalInboxSettings:
    """Single-owner reviewer identity and bearer credential."""

    owner_id: str
    actor_id: str
    reviewer_token: str

    def __post_init__(self) -> None:
        if not self.owner_id.strip() or not self.actor_id.strip():
            raise ValueError("approval owner and actor IDs are required")
        if not self.reviewer_token.strip():
            raise ValueError("reviewer_token must not be empty")


def create_approval_inbox_router(
    settings: ApprovalInboxSettings,
    connection_factory: ConnectionFactory,
    *,
    id_factory: IdFactory = new_ulid,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Create browser shell plus authenticated approval endpoints."""

    router = APIRouter()

    @router.get("/approval-inbox", response_class=HTMLResponse, include_in_schema=False)
    def approval_inbox_page() -> HTMLResponse:
        return HTMLResponse(_WEB_PAGE.read_text(encoding="utf-8"))

    @router.get("/api/approval-inbox", response_model=list[ApprovalInboxItem])
    def list_approval_inbox(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> list[ApprovalInboxItem]:
        authorize_reviewer(authorization, settings.reviewer_token)
        with connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT finding.public_id, run.public_id, repository.full_name,
                       pull_request.github_number, run.head_sha, finding.claim,
                       finding.evidence, run.cost_budget_usd_micros, approval.decision
                FROM findings AS finding
                JOIN runs AS run
                  ON run.id = finding.run_id
                 AND run.owner_id = finding.owner_id
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                 AND pull_request.owner_id = run.owner_id
                JOIN repositories AS repository
                  ON repository.id = pull_request.repository_id
                 AND repository.owner_id = run.owner_id
                LEFT JOIN approvals AS approval
                  ON approval.finding_id = finding.id
                 AND approval.owner_id = finding.owner_id
                WHERE finding.owner_id = %s
                  AND run.state = 'awaiting_approval'
                  AND pull_request.head_sha = run.head_sha
                ORDER BY run.created_at, finding.id
                """,
                (settings.owner_id,),
            ).fetchall()
        return [_inbox_item(row) for row in rows]

    @router.post(
        "/api/approval-inbox/{finding_id}/decision",
        response_model=ApprovalDecisionReceipt,
    )
    def record_approval_decision(
        finding_id: str,
        request: ApprovalDecisionRequest,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> ApprovalDecisionReceipt:
        authorize_reviewer(authorization, settings.reviewer_token)
        decided_at = now().astimezone(UTC)
        with connection_factory() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT finding.id, finding.public_id, run.id, run.public_id, run.head_sha,
                       run.state, pull_request.head_sha, approval.public_id,
                       approval.actor_id, approval.decision, approval.reason,
                       approval.decided_at
                FROM findings AS finding
                JOIN runs AS run
                  ON run.id = finding.run_id
                 AND run.owner_id = finding.owner_id
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                 AND pull_request.owner_id = run.owner_id
                LEFT JOIN approvals AS approval
                  ON approval.finding_id = finding.id
                 AND approval.owner_id = finding.owner_id
                WHERE finding.owner_id = %s AND finding.public_id = %s
                FOR UPDATE OF finding, run, pull_request
                """,
                (settings.owner_id, finding_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")

            (
                internal_finding_id,
                public_finding_id,
                internal_run_id,
                public_run_id,
                run_head_sha,
                run_state,
                current_head_sha,
                existing_approval_id,
                existing_actor_id,
                existing_decision,
                existing_reason,
                existing_decided_at,
            ) = row
            if request.head_sha != run_head_sha or current_head_sha != run_head_sha:
                raise HTTPException(status.HTTP_409_CONFLICT, "finding commit is stale")
            if run_state != "awaiting_approval":
                raise HTTPException(status.HTTP_409_CONFLICT, "finding is not awaiting approval")
            if existing_approval_id is not None:
                if (
                    existing_actor_id == settings.actor_id
                    and existing_decision == request.decision.value
                    and existing_reason == request.reason
                ):
                    return _receipt(
                        existing_approval_id,
                        public_finding_id,
                        public_run_id,
                        run_head_sha,
                        request.decision,
                        existing_decided_at,
                        already_recorded=True,
                    )
                raise HTTPException(status.HTTP_409_CONFLICT, "finding already has a decision")

            approval_id = id_factory()
            connection.execute(
                """
                INSERT INTO approvals (
                    public_id, owner_id, run_id, finding_id, actor_id,
                    decision, reason, head_sha, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    approval_id,
                    settings.owner_id,
                    internal_run_id,
                    internal_finding_id,
                    settings.actor_id,
                    request.decision.value,
                    request.reason,
                    run_head_sha,
                    decided_at,
                ),
            )
            approval_command = ApprovalCommand(
                schema_version="1",
                public_id=approval_id,
                owner_id=settings.owner_id,
                run_id=public_run_id,
                head_sha=run_head_sha,
                finding_id=public_finding_id,
                actor_id=settings.actor_id,
                decision=request.decision,
                reason=request.reason,
                decided_at=decided_at,
            )
            connection.execute(
                """
                INSERT INTO run_events (
                    public_id, owner_id, run_id, event_key, event_type,
                    event_data, occurred_at
                ) VALUES (%s, %s, %s, %s, 'approval.signal_created', %s::jsonb, %s)
                """,
                (
                    id_factory(),
                    settings.owner_id,
                    internal_run_id,
                    f"approval:{approval_id}:signal",
                    approval_command.model_dump_json(),
                    decided_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_events (
                    public_id, owner_id, run_id, event_key, event_type,
                    event_data, occurred_at
                ) VALUES (%s, %s, %s, %s, 'approval.decision_recorded', %s::jsonb, %s)
                """,
                (
                    id_factory(),
                    settings.owner_id,
                    internal_run_id,
                    f"approval:{approval_id}",
                    json.dumps(
                        {
                            "approval_id": approval_id,
                            "finding_id": public_finding_id,
                            "head_sha": run_head_sha,
                            "decision": request.decision.value,
                        }
                    ),
                    decided_at,
                ),
            )
        return _receipt(
            approval_id,
            public_finding_id,
            public_run_id,
            run_head_sha,
            request.decision,
            decided_at,
            already_recorded=False,
        )

    return router


def _inbox_item(row: tuple[Any, ...]) -> ApprovalInboxItem:
    (
        finding_id,
        run_id,
        repository_full_name,
        pull_request_number,
        head_sha,
        claim,
        raw_evidence,
        cost_budget_usd_micros,
        decision,
    ) = row
    evidence = tuple(Evidence.model_validate(item) for item in raw_evidence)
    return ApprovalInboxItem(
        schema_version="1",
        finding_id=finding_id,
        run_id=run_id,
        repository_full_name=repository_full_name,
        pull_request_number=pull_request_number,
        head_sha=head_sha,
        claim=claim,
        evidence=evidence,
        verification=_verification_status(evidence),
        cost_usd_micros=None,
        cost_budget_usd_micros=cost_budget_usd_micros,
        status=FindingApprovalStatus(decision or FindingApprovalStatus.PENDING),
    )


def _verification_status(evidence: tuple[Evidence, ...]) -> VerificationStatus:
    test_results = [item for item in evidence if item.kind is EvidenceKind.TEST_RESULT]
    if not test_results:
        return VerificationStatus.NOT_RECORDED
    if any(item.exit_code != 0 for item in test_results):
        return VerificationStatus.FAILED
    return VerificationStatus.PASSED


def _receipt(
    approval_id: str,
    finding_id: str,
    run_id: str,
    head_sha: str,
    decision: ApprovalDecision,
    decided_at: datetime,
    *,
    already_recorded: bool,
) -> ApprovalDecisionReceipt:
    return ApprovalDecisionReceipt(
        schema_version="1",
        approval_id=approval_id,
        finding_id=finding_id,
        run_id=run_id,
        head_sha=head_sha,
        decision=decision,
        decided_at=decided_at,
        already_recorded=already_recorded,
    )
