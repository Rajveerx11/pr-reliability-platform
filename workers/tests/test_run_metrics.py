"""Tests for run_metrics persistence in record_terminal and _write_run_metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from pr_reliability_workers.providers.operations import (
    _usage_data,
    _usage_from_data,
    _write_run_metrics,
)
from pr_reliability_workers.workflows.types import ModelUsage, TerminalRequest, WorkflowOutcome

# ---------------------------------------------------------------------------
# _usage_data / _usage_from_data round-trip
# ---------------------------------------------------------------------------


def test_usage_data_none_returns_none() -> None:
    assert _usage_data(None) is None


def test_usage_data_serializes_all_fields() -> None:
    usage = ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd_micros=200)
    data = _usage_data(usage)
    assert data == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd_micros": 200,
        "total_tokens": 15,
    }


def test_usage_from_data_none_returns_none() -> None:
    assert _usage_from_data(None) is None


def test_usage_from_data_round_trip() -> None:
    usage = ModelUsage(input_tokens=7, output_tokens=3, total_tokens=10)
    result = _usage_from_data(_usage_data(usage))
    assert result is not None
    assert result.input_tokens == 7
    assert result.output_tokens == 3
    assert result.total_tokens == 10
    assert result.cost_usd_micros is None


def test_usage_from_data_raises_on_non_dict() -> None:
    with pytest.raises(TypeError):
        _usage_from_data([1, 2, 3])


# ---------------------------------------------------------------------------
# _write_run_metrics: coverage logic
# ---------------------------------------------------------------------------


def _terminal_request(
    outcome: WorkflowOutcome = WorkflowOutcome.PUBLISHED,
    run_duration_ms: int | None = None,
    approval_wait_ms: int | None = None,
    usage: ModelUsage | None = None,
) -> TerminalRequest:
    run_id = "R" * 26
    head_sha = "a" * 40
    return TerminalRequest(
        owner_id="O" * 26,
        run_id=run_id,
        head_sha=head_sha,
        outcome=outcome,
        reason=None,
        idempotency_key=f"{run_id}:{head_sha}:terminal:{outcome.value}",
        run_duration_ms=run_duration_ms,
        approval_wait_ms=approval_wait_ms,
        usage=usage,
    )


def test_write_run_metrics_full_coverage_executes_upsert() -> None:
    """full coverage: all three token fields known."""
    connection = MagicMock()
    request = _terminal_request(
        usage=ModelUsage(input_tokens=100, output_tokens=40, total_tokens=140),
        run_duration_ms=5000,
        approval_wait_ms=1200,
    )
    _write_run_metrics(connection, "O" * 26, 1, "published", request)
    assert connection.execute.call_count == 2
    sql, args = connection.execute.call_args[0]
    assert "run_metrics" in sql
    assert "ON CONFLICT" in sql
    # coverage: full because 3 of 3 token fields are known
    assert "full" in args


def test_write_run_metrics_partial_coverage() -> None:
    """partial coverage: only input_tokens known."""
    connection = MagicMock()
    request = _terminal_request(usage=ModelUsage(input_tokens=50))
    _write_run_metrics(connection, "O" * 26, 1, "failed", request)
    args = connection.execute.call_args.args[1]
    assert "partial" in args


def test_write_run_metrics_unknown_coverage_when_no_usage() -> None:
    """unknown coverage: usage is None."""
    connection = MagicMock()
    request = _terminal_request()
    _write_run_metrics(connection, "O" * 26, 1, "cancelled", request)
    args = connection.execute.call_args.args[1]
    assert args[-1] is None  # usage_coverage is NULL when no usage object


def test_write_run_metrics_unknown_coverage_when_usage_empty() -> None:
    """unknown coverage: ModelUsage exists but all fields are None."""
    connection = MagicMock()
    request = _terminal_request(usage=ModelUsage())
    _write_run_metrics(connection, "O" * 26, 1, "published", request)
    args = connection.execute.call_args.args[1]
    assert "unknown" in args


def test_write_run_metrics_null_duration_stays_null() -> None:
    """Missing duration must remain NULL, never become zero."""
    connection = MagicMock()
    request = _terminal_request(run_duration_ms=None, approval_wait_ms=None)
    _write_run_metrics(connection, "O" * 26, 1, "failed", request)
    args = connection.execute.call_args.args[1]
    q, c, m, v, app, pub, total = args[3:10]
    assert total is None
    assert app is None
    assert q is None
    assert c is None
    assert m is None
    assert v is None
    assert pub is None


def test_write_run_metrics_derives_stage_durations_from_events() -> None:
    """Per-stage durations are computed from run_events timestamps."""
    t0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    events = [
        ("run.command_created", t0),
        ("run.command_dispatched", t0 + timedelta(milliseconds=150)),
        ("activity.select_context.completed", t0 + timedelta(milliseconds=650)),
        ("activity.analyze.completed", t0 + timedelta(milliseconds=2650)),
        ("activity.verify.completed", t0 + timedelta(milliseconds=4650)),
        ("approval.decision_recorded", t0 + timedelta(milliseconds=300)),
        ("approval.decision_recorded", t0 + timedelta(milliseconds=4700)),
        ("run.command_dispatched", t0 + timedelta(milliseconds=4750)),
        ("github.review_publish_started", t0 + timedelta(milliseconds=5000)),
        ("github.review_published", t0 + timedelta(milliseconds=5500)),
    ]
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [
        (
            name,
            when,
            {
                "publish_started_at": (t0 + timedelta(milliseconds=5000)).isoformat(),
                "publish_finished_at": (t0 + timedelta(milliseconds=5500)).isoformat(),
            }
            if name == "github.review_published"
            else {},
        )
        for name, when in events
    ]

    request = _terminal_request(
        run_duration_ms=6000,
        approval_wait_ms=350,
    )
    _write_run_metrics(connection, "O" * 26, 1, "published", request)

    args = connection.execute.call_args.args[1]
    q, c, m, v, app, pub, total = args[3:10]
    assert q == 150
    assert c == 500
    assert m == 2000
    assert v == 2000
    assert app == 350
    assert pub == 500
    assert total == 6000
    assert args[10:13] == (None, None, None)  # terminal/check attempts are not finished


def test_write_run_metrics_unknown_attempts_remain_null() -> None:
    connection = MagicMock()
    _write_run_metrics(connection, "O" * 26, 1, "failed", _terminal_request())
    assert connection.execute.call_args.args[1][10:13] == (None, None, None)


def test_write_run_metrics_publish_without_start_is_unknown() -> None:
    connection = MagicMock()
    t0 = datetime(2026, 9, 13, tzinfo=UTC)
    connection.execute.return_value.fetchall.return_value = [
        ("approval.decision_recorded", t0, {}),
        ("approval.decision_recorded", t0 + timedelta(hours=2), {}),
        ("run.command_dispatched", t0 + timedelta(hours=3), {}),
        ("github.review_published", t0 + timedelta(hours=4), {}),
    ]
    _write_run_metrics(connection, "O" * 26, 1, "published", _terminal_request())
    assert connection.execute.call_args.args[1][8] is None


def test_write_run_metrics_out_of_order_events_are_unknown() -> None:
    connection = MagicMock()
    t0 = datetime(2026, 9, 13, tzinfo=UTC)
    connection.execute.return_value.fetchall.return_value = [
        ("run.command_created", t0 + timedelta(seconds=2), {}),
        ("run.command_dispatched", t0 + timedelta(seconds=1), {}),
        ("github.review_publish_started", t0 + timedelta(seconds=3), {}),
        ("github.review_published", t0 + timedelta(seconds=2), {}),
    ]
    _write_run_metrics(connection, "O" * 26, 1, "published", _terminal_request())
    args = connection.execute.call_args.args[1]
    assert args[3] is None
    assert args[8] is None
