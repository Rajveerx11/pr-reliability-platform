"""Strict input and output models for version-one evaluation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ReplayFinding(EvaluationModel):
    """Previously adjudicated finding used by a deterministic replay."""

    summary: str = Field(min_length=1)
    matched_defect_index: StrictInt | None = Field(default=None, ge=0)


class ReplayUsage(EvaluationModel):
    """Recorded provider usage; unknown values remain null."""

    coverage: Literal["full", "partial", "unknown"]
    prompt_tokens: StrictInt | None = Field(default=None, ge=0)
    completion_tokens: StrictInt | None = Field(default=None, ge=0)
    total_tokens: StrictInt | None = Field(default=None, ge=0)
    reported_cost_usd_micros: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_coverage(self) -> ReplayUsage:
        values = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        known = sum(value is not None for value in values)
        expected = "unknown" if known == 0 else "full" if known == 3 else "partial"
        if self.coverage != expected:
            raise ValueError(f"usage coverage must be {expected}")
        if self.total_tokens is not None:
            for component in (self.prompt_tokens, self.completion_tokens):
                if component is not None and self.total_tokens < component:
                    raise ValueError("total_tokens cannot be below a known component")
        if known == 3 and self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens must equal prompt plus completion tokens")
        return self


class ReplayAttempt(EvaluationModel):
    task_id: str = Field(min_length=1)
    status: Literal["completed", "failed", "timeout", "not_run"]
    findings: tuple[ReplayFinding, ...]
    agent_duration_ms: StrictInt | None = Field(default=None, ge=0)
    end_to_end_latency_ms: StrictInt | None = Field(default=None, ge=0)
    usage: ReplayUsage
    retries: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def keep_not_run_measurements_unknown(self) -> ReplayAttempt:
        if self.status != "not_run":
            return self
        if (
            self.findings
            or self.agent_duration_ms is not None
            or self.end_to_end_latency_ms is not None
        ):
            raise ValueError("not_run attempts cannot contain findings or measured durations")
        if (
            self.usage.coverage != "unknown"
            or self.usage.reported_cost_usd_micros is not None
            or self.retries != 0
        ):
            raise ValueError("not_run attempts must keep usage and cost unknown and retries zero")
        return self


class EvaluationLimits(EvaluationModel):
    context_token_budget: StrictInt | None = Field(default=None, ge=1)
    verifier_timeout_seconds: StrictInt = Field(ge=1, le=60)
    sandbox_enabled: bool
    proof_gate_enabled: bool


class ReplayManifest(EvaluationModel):
    """Complete immutable input for one deterministic replay."""

    schema_version: Literal[1]
    run_mode: Literal["deterministic_replay"]
    label: str = Field(min_length=1)
    recorded_at: datetime
    evaluated_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    corpus_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: None = None
    model: None = None
    limits: EvaluationLimits
    attempts: tuple[ReplayAttempt, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_tasks(self) -> ReplayManifest:
        task_ids = [attempt.task_id for attempt in self.attempts]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("replay task IDs must be unique")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must include a time zone")
        return self
