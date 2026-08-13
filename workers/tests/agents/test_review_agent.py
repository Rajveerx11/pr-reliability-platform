"""Adapter tests for the provider-neutral single review agent."""

import json
from collections.abc import Iterator

import pytest
from pr_reliability_contracts import (
    ModelUsage,
    ReviewCommand,
    UsageCoverage,
)
from pr_reliability_workers.agents import (
    InvalidModelOutput,
    ModelCallFailed,
    ModelRequest,
    ModelResponse,
    ReviewAgent,
)
from pydantic import ValidationError

PUBLIC_ID = "01J00000000000000000000001"
RESULT_ID = "01J00000000000000000000002"
OWNER_ID = "01J00000000000000000000003"
RUN_ID = "01J00000000000000000000004"
HEAD_SHA = "a" * 40


class FakeClient:
    def __init__(self, response: ModelResponse | Exception) -> None:
        self.response = response
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def full_usage() -> ModelUsage:
    return ModelUsage(
        schema_version="1",
        coverage=UsageCoverage.FULL,
        prompt_tokens=120,
        completion_tokens=30,
        total_tokens=150,
        reported_cost_usd_micros=2_345,
    )


def review_command() -> ReviewCommand:
    return ReviewCommand(
        schema_version="1",
        public_id=PUBLIC_ID,
        result_public_id=RESULT_ID,
        owner_id=OWNER_ID,
        run_id=RUN_ID,
        head_sha=HEAD_SHA,
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


def clock(values: list[int]) -> Iterator[int]:
    yield from values


def test_returns_only_valid_structured_findings_and_call_facts() -> None:
    response = ModelResponse(
        output_json=json.dumps({"findings": [valid_finding()]}),
        usage=full_usage(),
    )
    client = FakeClient(response)
    times = clock([1_000_000, 3_500_000])

    result = ReviewAgent(client, monotonic_ns=lambda: next(times)).review(
        review_command(), '{"path":"app.py","content":"code"}\n'
    )

    assert len(result.findings) == 1
    assert result.findings[0].claim == "Retry can create a duplicate record."
    assert result.findings[0].owner_id == OWNER_ID
    assert result.findings[0].run_id == RUN_ID
    assert result.findings[0].head_sha == HEAD_SHA
    assert result.findings[0].public_id.startswith(RESULT_ID[:10])
    assert result.duration_ms == 3
    assert result.usage.reported_cost_usd_micros == 2_345
    assert result.usage.coverage is UsageCoverage.FULL
    assert client.requests[0].output_schema["additionalProperties"] is False
    assert "external writes" in client.requests[0].instruction


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        json.dumps({"findings": [valid_finding()], "extra": True}),
        json.dumps({"findings": [{**valid_finding(), "confidence": 2}]}),
    ],
)
def test_invalid_output_fails_closed(output: str) -> None:
    client = FakeClient(ModelResponse(output_json=output, usage=full_usage()))

    with pytest.raises(InvalidModelOutput):
        ReviewAgent(client).review(review_command(), "context")


def test_model_cannot_supply_trusted_run_identity() -> None:
    finding = {**valid_finding(), "run_id": "01J00000000000000000000009"}
    client = FakeClient(
        ModelResponse(output_json=json.dumps({"findings": [finding]}), usage=full_usage())
    )

    with pytest.raises(InvalidModelOutput, match="validation"):
        ReviewAgent(client).review(review_command(), "context")


def test_duplicate_finding_identity_fails_closed() -> None:
    client = FakeClient(
        ModelResponse(
            output_json=json.dumps({"findings": [valid_finding(), valid_finding()]}),
            usage=full_usage(),
        )
    )

    with pytest.raises(InvalidModelOutput, match="validation"):
        ReviewAgent(client).review(review_command(), "context")


def test_finding_identity_is_stable_when_model_reorders_output() -> None:
    first = valid_finding()
    second = {**valid_finding(), "claim": "A second supported defect."}
    responses = ([first, second], [second, first])
    mapped_ids: list[dict[str, str]] = []

    for findings in responses:
        client = FakeClient(
            ModelResponse(
                output_json=json.dumps({"findings": findings}),
                usage=full_usage(),
            )
        )
        result = ReviewAgent(client).review(review_command(), "context")
        mapped_ids.append({finding.claim: finding.public_id for finding in result.findings})

    assert mapped_ids[0] == mapped_ids[1]


def test_provider_failure_returns_no_partial_result() -> None:
    client = FakeClient(RuntimeError("provider detail that must not escape"))

    with pytest.raises(ModelCallFailed, match="model client failed") as raised:
        ReviewAgent(client).review(review_command(), "context")

    assert "provider detail" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_invalid_output_does_not_remain_in_exception_chain() -> None:
    client = FakeClient(
        ModelResponse(output_json='{"findings":[],"secret":"TOP_SECRET"}', usage=full_usage())
    )

    with pytest.raises(InvalidModelOutput) as raised:
        ReviewAgent(client).review(review_command(), "context")

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("coverage", "prompt", "completion", "total"),
    [
        (UsageCoverage.UNKNOWN, None, None, None),
        (UsageCoverage.PARTIAL, 10, None, None),
        (UsageCoverage.FULL, 10, 5, 15),
    ],
)
def test_usage_coverage_preserves_unknown_values(
    coverage: UsageCoverage,
    prompt: int | None,
    completion: int | None,
    total: int | None,
) -> None:
    usage = ModelUsage(
        schema_version="1",
        coverage=coverage,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        reported_cost_usd_micros=None,
    )

    assert usage.prompt_tokens is prompt
    assert usage.reported_cost_usd_micros is None


def test_usage_rejects_incorrect_coverage_or_total() -> None:
    with pytest.raises(ValidationError):
        ModelUsage(schema_version="1", coverage=UsageCoverage.FULL)
    with pytest.raises(ValidationError, match="total_tokens"):
        ModelUsage(
            schema_version="1",
            coverage=UsageCoverage.FULL,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=14,
        )
    with pytest.raises(ValidationError, match="known component"):
        ModelUsage(
            schema_version="1",
            coverage=UsageCoverage.PARTIAL,
            prompt_tokens=10,
            completion_tokens=None,
            total_tokens=4,
        )
