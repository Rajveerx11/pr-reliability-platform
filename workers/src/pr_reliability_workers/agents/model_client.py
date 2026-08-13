"""Narrow interface implemented by hosted or local model providers."""

from dataclasses import dataclass
from typing import Any, Protocol

from pr_reliability_contracts import ModelUsage


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Provider-neutral model input and required JSON schema."""

    instruction: str
    context: str
    output_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Raw model output plus exact provider-reported usage facts."""

    output_json: str
    usage: ModelUsage


class ModelClient(Protocol):
    """The only model-provider surface visible to review code."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return JSON matching the request schema or raise a provider error."""

        ...
