"""Historical model evidence must never become a platform verification result."""

import pytest
from pr_reliability_api.approvals.inbox import _inbox_item
from pr_reliability_api.dashboard.routes import _finding
from pr_reliability_contracts import Evidence
from pydantic import ValidationError


@pytest.mark.parametrize("kind", ["test_result", "reproduction", "source_location"])
@pytest.mark.parametrize("exit_code", [0, 1, -1])
def test_historical_evidence_is_an_unverified_claim(kind, exit_code):
    raw = {
        "schema_version": "1",
        "kind": kind,
        "summary": "The model says this passed",
        "file_path": "app.py",
        "command": ["pytest"],
        "exit_code": exit_code,
    }
    finding_id, run_id = "01J00000000000000000000001", "01J00000000000000000000002"
    inbox = _inbox_item((finding_id, run_id, "owner/repo", 1, "a" * 40, "Claim", [raw], 100, None))
    dashboard = _finding((finding_id, "correctness", "high", "Claim", 0.9, [raw], "pending"))
    for item in [inbox, dashboard]:
        assert item.verification == "claimed"
        assert item.evidence[0].model_dump()["verification"] == "claimed"
        assert item.evidence[0].exit_code == exit_code


@pytest.mark.parametrize("verification", ["passed", "failed", "verified"])
def test_model_evidence_cannot_supply_a_platform_status(verification):
    with pytest.raises(ValidationError):
        Evidence.model_validate(
            {
                "schema_version": "1",
                "kind": "test_result",
                "summary": "Fabricated result",
                "command": ["pytest"],
                "exit_code": 0,
                "verification": verification,
            }
        )
