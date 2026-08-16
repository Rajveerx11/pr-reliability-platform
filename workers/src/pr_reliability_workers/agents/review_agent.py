"""Validate one model call into replay-safe structured findings."""

from __future__ import annotations

import time
from collections.abc import Callable
from hashlib import sha256

from pr_reliability_contracts import (
    Evidence,
    Finding,
    FindingSeverity,
    NonEmptyText,
    ReviewCommand,
    ReviewResult,
)
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, ValidationError, model_validator

from .model_client import ModelClient, ModelRequest

_INSTRUCTION = """Review supplied pull request context for correctness defects.
Return only findings supported by the supplied context. Do not propose or perform external writes.
Each finding must match the supplied JSON schema exactly. Return an empty findings list when no
supported defect exists."""
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class InvalidModelOutput(RuntimeError):
    """Model returned output that cannot cross contract boundary."""


class ModelCallFailed(RuntimeError):
    """Provider call failed before a valid result existed."""


class _ProposedFinding(BaseModel):
    """Only untrusted finding content; workflow identity is attached later."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    category: NonEmptyText
    severity: FindingSeverity
    claim: NonEmptyText
    confidence: StrictFloat = Field(ge=0, le=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class _FindingsEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: tuple[_ProposedFinding, ...]

    @model_validator(mode="after")
    def reject_duplicates(self) -> _FindingsEnvelope:
        serialized = [finding.model_dump_json() for finding in self.findings]
        if len(serialized) != len(set(serialized)):
            raise ValueError("proposed findings must be unique")
        return self


class ReviewAgent:
    """One review agent with no provider-specific dependencies."""

    def __init__(
        self,
        client: ModelClient,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._client = client
        self._monotonic_ns = monotonic_ns

    def review(self, command: ReviewCommand, context: str) -> ReviewResult:
        """Run once and bind trusted run identity after validating model content."""

        if not context.strip():
            raise ValueError("context must not be empty")
        request = ModelRequest(
            instruction=_INSTRUCTION,
            context=context,
            output_schema=_FindingsEnvelope.model_json_schema(),
        )
        started_ns = self._monotonic_ns()
        response = None
        try:
            response = self._client.complete(request)
        except Exception:  # noqa: BLE001 -- provider adapters may raise arbitrary SDK errors
            response = None
        if response is None:
            raise ModelCallFailed("model client failed") from None
        duration_ms = max(0, (self._monotonic_ns() - started_ns + 999_999) // 1_000_000)

        envelope = None
        try:
            envelope = _FindingsEnvelope.model_validate_json(response.output_json)
        except (ValidationError, ValueError):
            envelope = None
        if envelope is None:
            raise InvalidModelOutput("model output failed validation") from None

        findings = tuple(
            Finding(
                schema_version="1",
                public_id=_finding_public_id(command.result_public_id, proposed),
                owner_id=command.owner_id,
                run_id=command.run_id,
                head_sha=command.head_sha,
                **proposed.model_dump(),
            )
            for proposed in envelope.findings
        )
        return ReviewResult(
            schema_version="1",
            public_id=command.result_public_id,
            owner_id=command.owner_id,
            run_id=command.run_id,
            head_sha=command.head_sha,
            findings=findings,
            usage=response.usage,
            duration_ms=duration_ms,
        )


def _finding_public_id(result_public_id: str, finding: _ProposedFinding) -> str:
    """Derive a stable ULID from result identity and canonical finding content."""

    entropy = int.from_bytes(
        sha256(result_public_id.encode() + b":" + finding.model_dump_json().encode()).digest()[:10],
        "big",
    )
    encoded = ""
    for _ in range(16):
        encoded = _CROCKFORD[entropy & 31] + encoded
        entropy >>= 5
    return result_public_id[:10] + encoded
