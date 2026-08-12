from datetime import UTC, datetime

import pytest
from conftest import ACTOR_ID, APPROVAL_ID, FINDING_ID, REPOSITORY_ID
from pr_reliability_contracts import ApprovalCommand, ApprovalDecision, PublishCommentCommand
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
