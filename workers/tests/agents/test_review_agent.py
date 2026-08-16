"""Dependency coverage for provider-neutral review agent."""

import json

import pytest
from pr_reliability_contracts import ModelUsage, ReviewCommand, UsageCoverage
from pr_reliability_workers.agents import (
    InvalidModelOutput,
    ModelCallFailed,
    ModelResponse,
    ReviewAgent,
)


class FakeClient:
    def __init__(self, output: str | Exception) -> None:
        self.output = output

    def complete(self, request: object) -> ModelResponse:
        if isinstance(self.output, Exception):
            raise self.output
        return ModelResponse(output_json=self.output, usage=unknown_usage())


def unknown_usage() -> ModelUsage:
    return ModelUsage(schema_version="1", coverage=UsageCoverage.UNKNOWN)


def command() -> ReviewCommand:
    return ReviewCommand(
        schema_version="1",
        public_id="01J00000000000000000000001",
        result_public_id="01J00000000000000000000002",
        owner_id="01J00000000000000000000003",
        run_id="01J00000000000000000000004",
        head_sha="a" * 40,
    )


def valid_finding() -> dict[str, object]:
    return {
        "category": "correctness",
        "severity": "high",
        "claim": "Retry can create a duplicate record.",
        "confidence": 0.9,
        "evidence": [
            {
                "schema_version": "1",
                "kind": "source_location",
                "summary": "Insert has no unique key.",
                "file_path": "app.py",
                "start_line": 8,
            }
        ],
    }


def test_returns_valid_finding_with_unknown_usage_preserved() -> None:
    client = FakeClient(json.dumps({"findings": [valid_finding()]}))

    result = ReviewAgent(client).review(command(), "context")

    assert result.findings[0].run_id == command().run_id
    assert result.usage.coverage is UsageCoverage.UNKNOWN
    assert result.usage.total_tokens is None
    assert result.usage.reported_cost_usd_micros is None


@pytest.mark.parametrize(
    "output",
    ["not json", json.dumps({"findings": [valid_finding()], "extra": True})],
)
def test_invalid_output_fails_closed(output: str) -> None:
    with pytest.raises(InvalidModelOutput, match="validation"):
        ReviewAgent(FakeClient(output)).review(command(), "context")


def test_provider_failure_returns_no_partial_result_or_provider_detail() -> None:
    with pytest.raises(ModelCallFailed, match="model client failed") as raised:
        ReviewAgent(FakeClient(RuntimeError("secret provider detail"))).review(command(), "context")

    assert "secret provider detail" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_full_usage_must_equal_known_components() -> None:
    usage = ModelUsage(
        schema_version="1",
        coverage=UsageCoverage.FULL,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        reported_cost_usd_micros=123,
    )

    assert usage.total_tokens == 15
    assert usage.reported_cost_usd_micros == 123
