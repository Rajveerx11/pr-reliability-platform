"""OpenAI Responses API adapter for the provider-neutral model boundary."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

import httpx
from pr_reliability_contracts import ModelUsage, UsageCoverage

from ..agents import ModelRequest, ModelResponse

_API_URL = "https://api.openai.com/v1/responses"


class OpenAIResponsesClient:
    """Request strict, stateless JSON and expose only exact provider usage."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int = 4_096,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("OpenAI API key is required")
        if not model or not model.strip():
            raise ValueError("OpenAI model is required")
        if max_output_tokens < 1:
            raise ValueError("OpenAI output token limit must be positive")
        if timeout_seconds <= 0:
            raise ValueError("OpenAI timeout must be positive")
        self._api_key = api_key
        self._model = model.strip()
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Create one non-stored response and reject malformed provider envelopes."""

        request_key = hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()
        payload = {
            "model": self._model,
            "instructions": request.instruction,
            "input": request.context,
            "max_output_tokens": self._max_output_tokens,
            "prompt_cache_key": request_key,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "pr_reliability_findings",
                    "schema": _strict_schema(request.output_schema),
                    "strict": True,
                }
            },
        }
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    _API_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": request_key,
                        "User-Agent": "pr-reliability-platform",
                    },
                    json=payload,
                )
            if response.is_error:
                raise RuntimeError
            body = _json_object(response)
            if (
                body.get("status") != "completed"
                or body.get("error") is not None
                or body.get("incomplete_details") is not None
                or _contains_refusal(body.get("output"))
            ):
                raise RuntimeError
            output_text = body.get("output_text")
            if not isinstance(output_text, str) or not output_text.strip():
                raise RuntimeError
            return ModelResponse(output_json=output_text, usage=_usage(body.get("usage")))
        except Exception:  # noqa: BLE001 -- provider transport errors are not stable
            raise RuntimeError("OpenAI request failed") from None


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(schema)

    def visit(value: object) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object" or "properties" in value:
                properties = value.get("properties")
                if not isinstance(properties, dict):
                    raise TypeError("finding schema object must declare properties")
                value["additionalProperties"] = False
                value["required"] = list(properties)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(normalized)
    return normalized


def _contains_refusal(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("type") == "refusal":
            return True
        return any(_contains_refusal(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_refusal(child) for child in value)
    return False


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError from exc
    if not isinstance(body, dict):
        raise TypeError
    return body


def _usage(raw: object) -> ModelUsage:
    if not isinstance(raw, dict):
        return ModelUsage(schema_version="1", coverage=UsageCoverage.UNKNOWN)
    prompt_tokens = _optional_nonnegative_int(raw.get("input_tokens"))
    completion_tokens = _optional_nonnegative_int(raw.get("output_tokens"))
    total_tokens = _optional_nonnegative_int(raw.get("total_tokens"))
    known = sum(value is not None for value in (prompt_tokens, completion_tokens, total_tokens))
    coverage = (
        UsageCoverage.UNKNOWN
        if known == 0
        else UsageCoverage.FULL
        if known == 3
        else UsageCoverage.PARTIAL
    )
    return ModelUsage(
        schema_version="1",
        coverage=coverage,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        reported_cost_usd_micros=None,
    )


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value
