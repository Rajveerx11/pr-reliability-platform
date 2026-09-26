"""Temporal history reconciliation includes the terminal and completed Check Run."""

from __future__ import annotations

import pytest
from pr_reliability_workers.activities.metrics import final_metrics
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory


def _history(*, check_attempt: int = 1, terminal_timeout: bool = False) -> WorkflowHistory:
    events = []
    for index, name in enumerate(("select_context", "record_terminal", "update_check"), 1):
        scheduled = index * 3
        event = HistoryEvent(event_id=scheduled)
        event.event_type = EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        event.activity_task_scheduled_event_attributes.activity_type.name = name
        events.append(event)
        event = HistoryEvent(event_id=scheduled + 1)
        event.event_type = EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
        event.activity_task_started_event_attributes.scheduled_event_id = scheduled
        event.activity_task_started_event_attributes.attempt = (
            check_attempt if name == "update_check" else 1
        )
        events.append(event)
        event = HistoryEvent(event_id=scheduled + 2)
        if terminal_timeout and name == "record_terminal":
            event.event_type = EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT
            event.activity_task_timed_out_event_attributes.scheduled_event_id = scheduled
        else:
            event.event_type = EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED
            event.activity_task_completed_event_attributes.scheduled_event_id = scheduled
        events.append(event)
    event = HistoryEvent(event_id=12)
    event.event_type = EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    event.activity_task_scheduled_event_attributes.activity_type.name = "finalize_metrics"
    events.append(event)
    return WorkflowHistory("workflow-1", events)


def test_final_counts_include_terminal_and_completed_check() -> None:
    result = final_metrics(_history(), "owner", "run", "sha")
    assert (result.activity_attempts, result.activity_retries, result.activity_timeouts) == (
        3,
        0,
        0,
    )


def test_successful_retry_cannot_prove_prior_timeout_count() -> None:
    result = final_metrics(_history(check_attempt=3), "owner", "run", "sha")
    assert (result.activity_attempts, result.activity_retries, result.activity_timeouts) == (
        5,
        2,
        None,
    )


def test_terminal_timeout_is_known_without_intermediate_retry() -> None:
    result = final_metrics(_history(terminal_timeout=True), "owner", "run", "sha")
    assert result.activity_timeouts == 1


def test_incomplete_history_fails_closed() -> None:
    history = _history()
    with pytest.raises(RuntimeError, match="incomplete"):
        final_metrics(WorkflowHistory("workflow-1", history.events[:-2]), "owner", "run", "sha")
