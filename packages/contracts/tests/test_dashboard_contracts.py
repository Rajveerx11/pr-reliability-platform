"""Contract tests for dashboard read models."""

from datetime import UTC, datetime

import pytest
from pr_reliability_contracts import DashboardOverview, DashboardRunSummary
from pydantic import ValidationError


def test_overview_keeps_unknown_usage_and_cost_explicit() -> None:
    overview = DashboardOverview(
        schema_version="1",
        total_runs=3,
        active_runs=1,
        awaiting_approval_runs=1,
        pending_findings=2,
        failed_runs=0,
        published_runs=1,
        p50_duration_ms=1_000,
        p95_duration_ms=2_000,
        usage_complete_runs=0,
        usage_partial_runs=0,
        usage_unknown_runs=3,
        exact_known_cost_usd_micros=None,
    )

    assert overview.usage_unknown_runs == overview.total_runs
    assert overview.exact_known_cost_usd_micros is None
    assert overview.activity_retry_count is None


def test_overview_rejects_negative_counts() -> None:
    with pytest.raises(ValidationError):
        DashboardOverview(
            schema_version="1",
            total_runs=-1,
            active_runs=0,
            awaiting_approval_runs=0,
            pending_findings=0,
            failed_runs=0,
            published_runs=0,
            usage_complete_runs=0,
            usage_partial_runs=0,
            usage_unknown_runs=0,
        )


def test_run_summary_requires_timezone_aware_timestamps() -> None:
    values = {
        "schema_version": "1",
        "run_id": "01J00000000000000000000001",
        "repository_full_name": "owner/repository",
        "pull_request_number": 17,
        "head_sha": "a" * 40,
        "generation": 1,
        "state": "published",
        "finding_count": 0,
        "pending_finding_count": 0,
        "duration_ms": 500,
        "created_at": "2026-08-30T10:00:00",
        "updated_at": datetime(2026, 8, 30, 10, 1, tzinfo=UTC),
    }

    with pytest.raises(ValidationError):
        DashboardRunSummary(**values)
