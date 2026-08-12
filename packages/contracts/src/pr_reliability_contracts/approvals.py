"""Human approval and external publish command contracts."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, StrictInt, field_validator

from .base import NonEmptyText, RunMessage, Ulid


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


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
