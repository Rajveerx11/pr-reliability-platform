"""Repository policy and bounded installation snapshots."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from .base import Contract


class RepositoryPolicy(Contract):
    enabled: StrictBool = True
    enabled_branches: list[str] = Field(default_factory=list, max_length=100)
    token_budget: StrictInt = Field(default=100_000, ge=1, le=2_147_483_647)
    cost_budget_usd_micros: StrictInt = Field(default=1_000_000, ge=0, le=9_223_372_036_854_775_807)
    # Repository-defined commands and additional profiles belong to issue #41.
    verification_profile: Literal["default"] = "default"

    @field_validator("enabled_branches")
    @classmethod
    def validate_branches(cls, branches: list[str]) -> list[str]:
        if len(set(branches)) != len(branches):
            raise ValueError("branches must be unique")
        for branch in branches:
            if not branch or len(branch) > 255 or any(ord(c) < 33 for c in branch):
                raise ValueError("branches must be nonempty exact branch names")
        return branches


class InstallationRepository(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: StrictInt = Field(gt=0)
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=256)
    default_branch: str = Field(min_length=1, max_length=255)


class InstallationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    installation_id: StrictInt = Field(gt=0)
    state: Literal["active", "suspended", "deleted"]
    repositories: list[InstallationRepository] = Field(default_factory=list, max_length=10_000)

    @field_validator("repositories")
    @classmethod
    def unique_repositories(cls, repositories):
        if len({repo.id for repo in repositories}) != len(repositories):
            raise ValueError("duplicate repository in snapshot")
        return repositories
