"""OpenAI Responses adapter contract tests without live credentials."""

from __future__ import annotations

import json

import httpx
import pytest
from pr_reliability_contracts import UsageCoverage
from pr_reliability_workers.agents import ModelRequest
from pr_reliability_workers.providers.openai import OpenAIResponsesClient


def request() -> ModelRequest:
    return ModelRequest(
        instruction="review",
        context="private source",
        output_schema={
            "type": "object",
            "default": {},
            "properties": {
                "findings": {
                    "type": "array",
                    "default": [],
                    "items": {
                        "type": "object",
                        "default": {},
                        "properties": {
                            "claim": {"type": "string", "default": ""},
                            "line": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        },
                    },
                }
            },
        },
        idempotency_key=f"{'R' * 26}:{'a' * 40}:analyze",
    )


def completed_response(**overrides):
    body = {
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [],
        "output_text": '{"findings":[]}',
        "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    }
    body.update(overrides)
    return body


def test_requests_stateless_strict_output_and_preserves_exact_usage() -> None:
    seen: dict[str, object] = {}

    def handler(incoming: httpx.Request) -> httpx.Response:
        seen["url"] = str(incoming.url)
        seen["authorization"] = incoming.headers["Authorization"]
        seen["idempotency"] = incoming.headers["Idempotency-Key"]
        seen["payload"] = json.loads(incoming.content)
        return httpx.Response(200, json=completed_response())

    result = OpenAIResponsesClient(
        "provider-secret",
        "configured-model",
        transport=httpx.MockTransport(handler),
    ).complete(request())

    payload = seen["payload"]
    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert seen["authorization"] == "Bearer provider-secret"
    assert payload["store"] is False
    assert payload["instructions"] == "review"
    assert payload["input"] == "private source"
    assert payload["prompt_cache_key"] == seen["idempotency"]
    response_format = payload["text"]["format"]
    assert response_format["strict"] is True
    schema = response_format["schema"]
    assert schema["required"] == ["findings"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["findings"]["items"]["required"] == ["claim", "line"]
    assert schema["properties"]["findings"]["items"]["additionalProperties"] is False
    _assert_no_defaults(schema)
    assert result.output_json == '{"findings":[]}'
    assert result.usage.coverage is UsageCoverage.FULL
    assert result.usage.prompt_tokens == 7
    assert result.usage.completion_tokens == 3
    assert result.usage.total_tokens == 10
    assert result.usage.reported_cost_usd_micros is None


def _assert_no_defaults(value: object) -> None:
    if isinstance(value, dict):
        assert "default" not in value
        for child in value.values():
            _assert_no_defaults(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_defaults(child)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "incomplete"},
        {"error": {"message": "provider secret detail"}},
        {"incomplete_details": {"reason": "limit"}},
        {"output": [{"content": [{"nested": {"type": "refusal"}}]}]},
        {"output_text": "  "},
        {"usage": {"input_tokens": True}},
    ],
)
def test_rejects_incomplete_refused_or_malformed_provider_responses(
    overrides: dict[str, object],
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=completed_response(**overrides))
    )

    with pytest.raises(RuntimeError, match="OpenAI request failed") as raised:
        OpenAIResponsesClient(
            "provider-secret",
            "configured-model",
            transport=transport,
        ).complete(request())

    serialized = repr(raised.value) + str(raised.value)
    assert "provider-secret" not in serialized
    assert "private source" not in serialized
    assert "provider secret detail" not in serialized
    assert raised.value.__cause__ is None


def test_missing_usage_remains_unknown_instead_of_becoming_zero() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=completed_response(usage=None))
    )

    usage = (
        OpenAIResponsesClient(
            "provider-secret",
            "configured-model",
            transport=transport,
        )
        .complete(request())
        .usage
    )

    assert usage.coverage is UsageCoverage.UNKNOWN
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.total_tokens is None
    assert usage.reported_cost_usd_micros is None
