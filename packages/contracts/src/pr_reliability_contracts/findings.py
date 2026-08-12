"""Structured agent findings and their checkable evidence."""

from enum import StrEnum

from pydantic import Field, StrictFloat, StrictInt, model_validator

from .base import Contract, NonEmptyText, RunMessage


class FindingSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvidenceKind(StrEnum):
    SOURCE_LOCATION = "source_location"
    REPRODUCTION = "reproduction"
    TEST_RESULT = "test_result"


class Evidence(Contract):
    kind: EvidenceKind
    summary: NonEmptyText
    file_path: NonEmptyText | None = None
    start_line: StrictInt | None = Field(default=None, ge=1)
    end_line: StrictInt | None = Field(default=None, ge=1)
    command: tuple[NonEmptyText, ...] | None = None
    exit_code: StrictInt | None = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "Evidence":
        if self.kind is EvidenceKind.SOURCE_LOCATION and self.file_path is None:
            raise ValueError("source_location evidence requires file_path")
        if self.start_line is not None and self.file_path is None:
            raise ValueError("line evidence requires file_path")
        if self.end_line is not None and self.start_line is None:
            raise ValueError("end_line requires start_line")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must not be before start_line")
        if self.kind in {EvidenceKind.REPRODUCTION, EvidenceKind.TEST_RESULT}:
            if not self.command:
                raise ValueError(f"{self.kind.value} evidence requires command")
            if self.exit_code is None:
                raise ValueError(f"{self.kind.value} evidence requires exit_code")
        return self


class Finding(RunMessage):
    category: NonEmptyText
    severity: FindingSeverity
    claim: NonEmptyText
    confidence: StrictFloat = Field(ge=0, le=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
