"""Tests for honest provider usage coverage."""

import pytest
from pr_reliability_workers.activities.review import _usage_status
from pr_reliability_workers.workflows import ModelUsage


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (None, "unknown"),
        (ModelUsage(), "unknown"),
        (ModelUsage(input_tokens=10), "partial"),
        (ModelUsage(output_tokens=5), "partial"),
        (ModelUsage(input_tokens=10, output_tokens=5), "complete"),
    ],
)
def test_usage_status_requires_both_token_counts(usage: ModelUsage | None, expected: str) -> None:
    assert _usage_status(usage) == expected
