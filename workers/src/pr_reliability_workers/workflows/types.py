"""Safe, replayable values stored in Temporal workflow history."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkflowOutcome(StrEnum):
    """Explicit terminal workflow outcomes."""

    PUBLISHED = "published"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True)
class ReviewWorkflowInput:
    owner_id: str
    run_id: str
    generation: int
    repository_id: str
    pull_request_id: str
    pull_request_number: int
    base_sha: str
    head_sha: str
    token_budget: int
    cost_budget_usd_micros: int
    approval_timeout_seconds: int = 86_400
    activity_timeout_seconds: int = 300


@dataclass(frozen=True)
class ApprovalSignal:
    run_id: str
    head_sha: str
    approved: bool
    finding_ids: tuple[str, ...] = ()
    approval_ids: tuple[str, ...] = ()
    comment_body_ref: str | None = None


@dataclass(frozen=True)
class SupersedeSignal:
    next_run: ReviewWorkflowInput


@dataclass(frozen=True)
class ReviewWorkflowStatus:
    run_id: str
    generation: int
    head_sha: str
    state: str
    cancellation_reason: str | None = None


@dataclass(frozen=True)
class ReviewWorkflowResult:
    run_id: str
    head_sha: str
    outcome: WorkflowOutcome
    reason: str | None = None


@dataclass(frozen=True)
class StageRequest:
    owner_id: str
    run_id: str
    head_sha: str
    idempotency_key: str
    input_ref: str | None = None
    base_sha: str | None = None


@dataclass(frozen=True)
class StageResult:
    output_ref: str


@dataclass(frozen=True)
class PublishRequest:
    owner_id: str
    run_id: str
    repository_id: str
    pull_request_number: int
    head_sha: str
    finding_ids: tuple[str, ...]
    approval_ids: tuple[str, ...]
    comment_body_ref: str
    idempotency_key: str


@dataclass(frozen=True)
class TerminalRequest:
    owner_id: str
    run_id: str
    head_sha: str
    outcome: WorkflowOutcome
    reason: str | None
    idempotency_key: str
