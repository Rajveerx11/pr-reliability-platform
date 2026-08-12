"""Shared valid identifiers for contract tests."""

from typing import Any

import pytest

PUBLIC_ID = "01J00000000000000000000001"
OWNER_ID = "01J00000000000000000000002"
RUN_ID = "01J00000000000000000000003"
REPOSITORY_ID = "01J00000000000000000000004"
PULL_REQUEST_ID = "01J00000000000000000000005"
FINDING_ID = "01J00000000000000000000006"
APPROVAL_ID = "01J00000000000000000000007"
ACTOR_ID = "01J00000000000000000000008"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


@pytest.fixture
def run_identity() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "public_id": PUBLIC_ID,
        "owner_id": OWNER_ID,
        "run_id": RUN_ID,
        "head_sha": HEAD_SHA,
    }
