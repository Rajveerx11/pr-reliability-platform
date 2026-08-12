"""Commands and states for one durable pull request review run."""

from enum import StrEnum

from pydantic import Field, StrictInt

from .base import GitSha, NonEmptyText, RunMessage, Ulid


class RunState(StrEnum):
    QUEUED = "queued"
    SELECTING_CONTEXT = "selecting_context"
    ANALYZING = "analyzing"
    VERIFYING = "verifying"
    AWAITING_APPROVAL = "awaiting_approval"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATES = frozenset(
    {RunState.PUBLISHED, RunState.REJECTED, RunState.FAILED, RunState.CANCELLED}
)

_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.SELECTING_CONTEXT, RunState.FAILED, RunState.CANCELLED}),
    RunState.SELECTING_CONTEXT: frozenset(
        {RunState.ANALYZING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.ANALYZING: frozenset({RunState.VERIFYING, RunState.FAILED, RunState.CANCELLED}),
    RunState.VERIFYING: frozenset(
        {RunState.AWAITING_APPROVAL, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.AWAITING_APPROVAL: frozenset(
        {RunState.PUBLISHED, RunState.REJECTED, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.PUBLISHED: frozenset(),
    RunState.REJECTED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


def can_transition(current: RunState, target: RunState) -> bool:
    """Return whether the run state machine permits this change."""

    return target in _ALLOWED_TRANSITIONS[current]


def require_transition(current: RunState, target: RunState) -> None:
    """Reject a state change that cannot be replayed safely."""

    if not can_transition(current, target):
        raise ValueError(f"run cannot transition from {current.value} to {target.value}")


class StartRunCommand(RunMessage):
    repository_id: Ulid
    pull_request_id: Ulid
    pull_request_number: StrictInt = Field(ge=1)
    base_sha: GitSha
    token_budget: StrictInt = Field(ge=1)
    cost_budget_usd_micros: StrictInt = Field(ge=0)


class CancelRunCommand(RunMessage):
    reason: NonEmptyText
    superseded_by_run_id: Ulid | None = None
