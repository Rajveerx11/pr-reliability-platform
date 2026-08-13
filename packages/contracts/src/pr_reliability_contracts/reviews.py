"""Commands and recorded results for one model-backed review."""

from enum import StrEnum

from pydantic import Field, StrictInt, model_validator

from .base import Contract, RunMessage, Ulid
from .findings import Finding


class UsageCoverage(StrEnum):
    """How completely the provider reported token usage."""

    FULL = "full"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ModelUsage(Contract):
    """Provider-reported usage without replacing unknown values with zero."""

    coverage: UsageCoverage
    prompt_tokens: StrictInt | None = Field(default=None, ge=0)
    completion_tokens: StrictInt | None = Field(default=None, ge=0)
    total_tokens: StrictInt | None = Field(default=None, ge=0)
    reported_cost_usd_micros: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_coverage(self) -> "ModelUsage":
        token_values = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        known = sum(value is not None for value in token_values)
        expected = (
            UsageCoverage.UNKNOWN
            if known == 0
            else UsageCoverage.FULL
            if known == len(token_values)
            else UsageCoverage.PARTIAL
        )
        if self.coverage is not expected:
            raise ValueError(f"usage coverage must be {expected.value}")
        if self.total_tokens is not None:
            for component in (self.prompt_tokens, self.completion_tokens):
                if component is not None and self.total_tokens < component:
                    raise ValueError("total_tokens cannot be below a known component")
        if known == len(token_values):
            assert self.prompt_tokens is not None
            assert self.completion_tokens is not None
            assert self.total_tokens is not None
            if self.total_tokens != self.prompt_tokens + self.completion_tokens:
                raise ValueError("total_tokens must equal prompt plus completion tokens")
        return self


class ReviewCommand(RunMessage):
    """Identity for one provider-neutral agent invocation."""

    result_public_id: Ulid


class ReviewResult(RunMessage):
    """Validated findings and measured call facts returned to the workflow."""

    findings: tuple[Finding, ...]
    usage: ModelUsage
    duration_ms: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def bind_findings_to_run(self) -> "ReviewResult":
        expected = (self.owner_id, self.run_id, self.head_sha)
        public_ids: set[str] = set()
        for finding in self.findings:
            actual = (finding.owner_id, finding.run_id, finding.head_sha)
            if actual != expected:
                raise ValueError("finding identity does not match review result")
            if finding.public_id in public_ids:
                raise ValueError("finding public IDs must be unique")
            public_ids.add(finding.public_id)
        return self
