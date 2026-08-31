from datetime import UTC, datetime

import pytest
from conftest import ACTOR_ID, APPROVAL_ID, FINDING_ID, HEAD_SHA, REPOSITORY_ID, RUN_ID
from pr_reliability_contracts import (
    ApprovalCommand,
    ApprovalDecision,
    ApprovalDecisionReceipt,
    ApprovalDecisionRequest,
    ApprovalInboxItem,
    Evidence,
    EvidenceKind,
    FindingApprovalStatus,
    PublishCommentCommand,
    VerificationStatus,
)
from pydantic import ValidationError


def test_approval_requires_aware_time_and_round_trips(run_identity: dict) -> None:
    approval = ApprovalCommand(
        **run_identity,
        finding_id=FINDING_ID,
        actor_id=ACTOR_ID,
        decision=ApprovalDecision.APPROVED,
        decided_at=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
    )

    assert ApprovalCommand.model_validate_json(approval.model_dump_json()) == approval

    with pytest.raises(ValidationError, match="timezone"):
        ApprovalCommand(
            **run_identity,
            finding_id=FINDING_ID,
            actor_id=ACTOR_ID,
            decision=ApprovalDecision.APPROVED,
            decided_at=datetime(2026, 8, 12, 7, 0),  # noqa: DTZ001 - deliberate invalid input
        )


def test_publish_command_requires_approval_and_finding(run_identity: dict) -> None:
    command = PublishCommentCommand(
        **run_identity,
        repository_id=REPOSITORY_ID,
        pull_request_number=17,
        finding_ids=(FINDING_ID,),
        approval_ids=(APPROVAL_ID,),
        idempotency_key="run:comment:head",
        body="Verified finding",
    )

    assert PublishCommentCommand.model_validate_json(command.model_dump_json()) == command

    with pytest.raises(ValidationError) as error:
        PublishCommentCommand(
            **run_identity,
            repository_id=REPOSITORY_ID,
            pull_request_number=17,
            finding_ids=(),
            approval_ids=(),
            idempotency_key="run:comment:head",
            body="Verified finding",
        )

    assert error.value.error_count() == 2


def test_approval_inbox_contract_shows_decision_context() -> None:
    item = ApprovalInboxItem(
        schema_version="1",
        finding_id=FINDING_ID,
        run_id=RUN_ID,
        repository_full_name="owner/repository",
        pull_request_number=17,
        head_sha=HEAD_SHA,
        claim="Null input crashes the request",
        evidence=(
            Evidence(
                schema_version="1",
                kind=EvidenceKind.TEST_RESULT,
                summary="Regression test passes",
                command=("pytest", "test_api.py"),
                exit_code=0,
            ),
        ),
        verification=VerificationStatus.PASSED,
        cost_usd_micros=None,
        cost_budget_usd_micros=500_000,
        status=FindingApprovalStatus.PENDING,
    )

    assert ApprovalInboxItem.model_validate_json(item.model_dump_json()) == item
    assert item.cost_usd_micros is None


def test_decision_request_and_receipt_bind_one_finding_to_head() -> None:
    request = ApprovalDecisionRequest(
        schema_version="1",
        head_sha=HEAD_SHA,
        decision=ApprovalDecision.APPROVED,
        reason="Evidence is sufficient",
    )
    receipt = ApprovalDecisionReceipt(
        schema_version="1",
        approval_id=APPROVAL_ID,
        finding_id=FINDING_ID,
        run_id=RUN_ID,
        head_sha=request.head_sha,
        decision=request.decision,
        decided_at=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        already_recorded=False,
    )

    assert receipt.head_sha == HEAD_SHA
    with pytest.raises(ValidationError, match="40 characters"):
        ApprovalDecisionRequest(
            schema_version="1",
            head_sha="stale",
            decision=ApprovalDecision.REJECTED,
        )
