"""Validated envelope for supported GitHub pull request webhooks."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, StrictInt, field_validator

from .base import Contract, GitSha, Ulid


class PullRequestAction(StrEnum):
    OPENED = "opened"
    REOPENED = "reopened"
    SYNCHRONIZE = "synchronize"
    CLOSED = "closed"


class PullRequestWebhook(Contract):
    public_id: Ulid
    owner_id: Ulid
    delivery_id: str = Field(min_length=1, max_length=128)
    installation_id: StrictInt = Field(ge=1)
    repository_github_id: StrictInt = Field(ge=1)
    pull_request_number: StrictInt = Field(ge=1)
    action: PullRequestAction
    base_sha: GitSha
    head_sha: GitSha
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must include a timezone")
        return value.astimezone(UTC)
