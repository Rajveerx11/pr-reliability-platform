"""Strict policy and snapshot boundaries."""

import pytest
from pr_reliability_contracts.repositories import InstallationSnapshot, RepositoryPolicy
from pydantic import ValidationError


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": "true"},
        {"token_budget": True},
        {"token_budget": 0},
        {"cost_budget_usd_micros": -1},
        {"enabled_branches": [""]},
        {"enabled_branches": ["main", "main"]},
        {"enabled_branches": ["bad\nbranch"]},
        {"verification_profile": "shell"},
        {"command": "rm"},
        {"schema_version": "2"},
    ],
)
def test_invalid_policy_rejected(changes):
    with pytest.raises(ValidationError):
        RepositoryPolicy.model_validate({"schema_version": "1", **changes})


def test_duplicate_inventory_rejected():
    repo = {"id": 1, "full_name": "owner/repo", "default_branch": "main"}
    with pytest.raises(ValidationError):
        InstallationSnapshot(installation_id=71, state="active", repositories=[repo, repo])
