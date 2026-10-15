"""Reconcile completed review activities from the authoritative Temporal history."""

from __future__ import annotations

from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory

from ..workflows.types import FinalMetricsRequest


def final_metrics(
    history: WorkflowHistory, owner_id: str, run_id: str, head_sha: str
) -> FinalMetricsRequest:
    """Count attempts before finalization; server-side retry failure types are not retained.

    Temporal folds intermediate retries into the final started event's attempt field.
    Therefore a retry makes the timeout total unknown, even when the last attempt
    succeeds: a previous retry might have been a timeout or a different failure.
    The finalization activity itself is excluded because it is still running.
    """
    scheduled: dict[int, str] = {}
    started: dict[int, int] = {}
    ended: set[int] = set()
    finalization_id: int | None = None
    timeouts = 0
    for event in history.events:
        kind = event.event_type
        if kind == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            name = event.activity_task_scheduled_event_attributes.activity_type.name
            if name == "finalize_metrics":
                finalization_id = event.event_id
                break
            scheduled[event.event_id] = name
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            details = event.activity_task_started_event_attributes
            started[details.scheduled_event_id] = details.attempt
        elif kind in (
            EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
            EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED,
            EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT,
            EventType.EVENT_TYPE_ACTIVITY_TASK_CANCELED,
        ):
            attribute = {
                EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED: "activity_task_completed_event_attributes",
                EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED: "activity_task_failed_event_attributes",
                EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT: "activity_task_timed_out_event_attributes",
                EventType.EVENT_TYPE_ACTIVITY_TASK_CANCELED: "activity_task_canceled_event_attributes",
            }[kind]
            details = getattr(event, attribute)
            ended.add(details.scheduled_event_id)
            if kind == EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT:
                timeouts += 1
    if finalization_id is None or not scheduled or set(scheduled) != ended:
        raise RuntimeError("review activity history is incomplete")
    if any(attempt < 1 for attempt in started.values()) or not set(started) <= set(scheduled):
        raise RuntimeError("review activity attempt history is invalid")
    attempts = sum(started.values())
    retries = sum(attempt - 1 for attempt in started.values())
    return FinalMetricsRequest(
        owner_id=owner_id,
        run_id=run_id,
        head_sha=head_sha,
        activity_attempts=attempts,
        activity_retries=retries,
        activity_timeouts=timeouts if retries == 0 else None,
    )
