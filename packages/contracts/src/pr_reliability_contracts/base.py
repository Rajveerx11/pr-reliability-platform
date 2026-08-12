"""Shared rules for every version-one message contract."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

SchemaVersion = Literal["1"]
Ulid = Annotated[
    str,
    StringConstraints(
        min_length=26,
        max_length=26,
        pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$",
    ),
]
GitSha = Annotated[
    str,
    StringConstraints(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$"),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Contract(BaseModel):
    """Immutable contract that rejects undeclared input."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    schema_version: SchemaVersion


class RunMessage(Contract):
    """Identity shared by messages that belong to one analyzed commit."""

    public_id: Ulid
    owner_id: Ulid
    run_id: Ulid
    head_sha: GitSha


class TimedRunMessage(RunMessage):
    """Run message with a timezone-aware event time."""

    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)
