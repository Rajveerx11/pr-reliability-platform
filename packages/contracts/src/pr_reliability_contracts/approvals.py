"""Human approval and external publish command contracts."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, StrictInt, field_validator, model_validator

from .base import Contract, GitSha, NonEmptyText, RunMessage, Ulid
from .findings import Evidence


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class FindingApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RECORDED = "not_recorded"


class ApprovalDecisionRequest(Contract):
    """One human decision bound to the commit shown in the browser."""

    head_sha: GitSha
    decision: ApprovalDecision
    reason: NonEmptyText | None = None


class ApprovalInboxItem(Contract):
    """Safe, complete review data shown before a human decides."""

    finding_id: Ulid
    run_id: Ulid
    repository_full_name: NonEmptyText
    pull_request_number: StrictInt = Field(ge=1)
    head_sha: GitSha
    claim: NonEmptyText
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    verification: VerificationStatus
    cost_usd_micros: StrictInt | None = Field(default=None, ge=0)
    cost_budget_usd_micros: StrictInt = Field(ge=0)
    status: FindingApprovalStatus


class ApprovalDecisionReceipt(Contract):
    approval_id: Ulid
    finding_id: Ulid
    run_id: Ulid
    head_sha: GitSha
    decision: ApprovalDecision
    decided_at: datetime
    already_recorded: bool

    @field_validator("decided_at")
    @classmethod
    def require_decision_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must include a timezone")
        return value.astimezone(UTC)


class ApprovalCommand(RunMessage):
    finding_id: Ulid
    actor_id: Ulid
    decision: ApprovalDecision
    reason: NonEmptyText | None = None
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must include a timezone")
        return value.astimezone(UTC)


class PublishCommentCommand(RunMessage):
    repository_id: Ulid
    pull_request_number: StrictInt = Field(ge=1)
    finding_ids: tuple[Ulid, ...] = Field(min_length=1)
    approval_ids: tuple[Ulid, ...] = Field(min_length=1)
    idempotency_key: NonEmptyText
    body: NonEmptyText

    @model_validator(mode="after")
    def require_stable_approval_mapping(self) -> "PublishCommentCommand":
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ValueError("finding_ids must be unique")
        if len(set(self.approval_ids)) != len(self.approval_ids):
            raise ValueError("approval_ids must be unique")
        if len(self.finding_ids) != len(self.approval_ids):
            raise ValueError("each finding needs one approval")
        expected_key = f"{self.run_id}:{self.head_sha}:publish"
        if self.idempotency_key != expected_key:
            raise ValueError("idempotency_key must bind run, head, and publish action")
        return self
