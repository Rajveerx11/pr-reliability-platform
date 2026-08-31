"""Safe read models for the authenticated review operations dashboard."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StrictFloat, StrictInt, StringConstraints, field_validator

from .approvals import VerificationStatus
from .base import Contract, GitSha, NonEmptyText, Ulid
from .findings import Evidence, FindingSeverity
from .runs import RunState

TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class DashboardStageName(StrEnum):
    WEBHOOK = "webhook"
    DISPATCH = "dispatch"
    SELECT_CONTEXT = "select_context"
    ANALYZE = "analyze"
    VERIFY = "verify"
    APPROVAL = "approval"
    PUBLISH = "publish"


class DashboardStageStatus(StrEnum):
    COMPLETED = "completed"
    CURRENT = "current"
    NOT_STARTED = "not_started"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class DashboardFindingStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NOT_ACTIONABLE = "not_actionable"


class DashboardOverview(Contract):
    total_runs: StrictInt = Field(ge=0)
    active_runs: StrictInt = Field(ge=0)
    awaiting_approval_runs: StrictInt = Field(ge=0)
    pending_findings: StrictInt = Field(ge=0)
    failed_runs: StrictInt = Field(ge=0)
    published_runs: StrictInt = Field(ge=0)
    p50_duration_ms: StrictInt | None = Field(default=None, ge=0)
    p95_duration_ms: StrictInt | None = Field(default=None, ge=0)
    activity_retry_count: StrictInt | None = Field(default=None, ge=0)
    usage_complete_runs: StrictInt = Field(ge=0)
    usage_partial_runs: StrictInt = Field(ge=0)
    usage_unknown_runs: StrictInt = Field(ge=0)
    exact_known_cost_usd_micros: StrictInt | None = Field(default=None, ge=0)


class DashboardRunSummary(Contract):
    run_id: Ulid
    repository_full_name: NonEmptyText
    pull_request_number: StrictInt = Field(ge=1)
    head_sha: GitSha
    generation: StrictInt = Field(ge=1)
    state: RunState
    finding_count: StrictInt = Field(ge=0)
    pending_finding_count: StrictInt = Field(ge=0)
    retry_count: StrictInt | None = Field(default=None, ge=0)
    duration_ms: StrictInt = Field(ge=0)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dashboard timestamps must include a timezone")
        return value.astimezone(UTC)


class DashboardRunPage(Contract):
    items: tuple[DashboardRunSummary, ...]
    total: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1, le=50)
    offset: StrictInt = Field(ge=0)


class DashboardTimelineEvent(Contract):
    event_type: NonEmptyText
    summary: NonEmptyText
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp must include a timezone")
        return value.astimezone(UTC)


class DashboardStage(Contract):
    name: DashboardStageName
    status: DashboardStageStatus


class DashboardFinding(Contract):
    finding_id: Ulid
    category: NonEmptyText
    severity: FindingSeverity
    claim: NonEmptyText
    confidence: StrictFloat = Field(ge=0, le=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    verification: VerificationStatus
    approval_status: DashboardFindingStatus


class DashboardRunDetail(Contract):
    run: DashboardRunSummary
    trace_id: TraceId | None = None
    stages: tuple[DashboardStage, ...]
    events: tuple[DashboardTimelineEvent, ...]
    findings: tuple[DashboardFinding, ...]
