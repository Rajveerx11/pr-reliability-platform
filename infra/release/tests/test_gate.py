"""Release publication rejects fabricated, stale, incomplete or cross-release staging evidence."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from infra.release import gate
from infra.release.manifest import ReleaseError, digest, read_json, write_json
from infra.release.staging import CHECKS
from infra.release.verify import STAGING_IDENTITY

NOW = datetime(2026, 10, 8, tzinfo=UTC)
PROGRAM_SHA = "a" * 64


def receipt_for(candidate, previous, root):
    receipt = root / "staging.json"
    write_json(
        receipt,
        {
            "schema_version": 1,
            "environment": "linux-staging",
            "commit": "2" * 40,
            "manifest_sha256": digest(candidate / "release.json"),
            "previous_manifest_sha256": digest(previous / "release.json"),
            "run_id": "123",
            "completed_at": NOW.isoformat(),
            "checks": dict.fromkeys(CHECKS, "passed"),
            "backup_manifest_sha256": "b" * 64,
            "e2e_program_sha256": PROGRAM_SHA,
        },
    )
    receipt.with_suffix(".sigstore.json").write_text("{}")
    return receipt


def test_complete_signed_staging_evidence_is_required(artifacts, tmp_path):
    _, candidate, previous = artifacts
    receipt = receipt_for(candidate, previous, tmp_path)
    commands = []
    assert (
        gate.release_gate(
            candidate, previous, receipt, PROGRAM_SHA, runner=commands.append, now=NOW
        )["commit"]
        == "2" * 40
    )
    command = commands[-1]
    assert command[command.index("--certificate-identity") + 1] == STAGING_IDENTITY
    assert command[command.index("--certificate-github-workflow-sha") + 1] == "2" * 40


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(environment="production"),
        lambda r: r.update(commit="1" * 40),
        lambda r: r.update(manifest_sha256="0" * 64),
        lambda r: r.update(previous_manifest_sha256="0" * 64),
        lambda r: r.update(backup_manifest_sha256=None),
        lambda r: r.update(e2e_program_sha256="0" * 64),
        lambda r: r.update(run_id=""),
        lambda r: r.update(schema_version=True),
        lambda r: r.update(checks={}),
        lambda r: r["checks"].update(restore="failed"),
        lambda r: r["checks"].pop("rollback_end_to_end"),
        lambda r: r.update(completed_at=(NOW - timedelta(hours=49)).isoformat()),
        lambda r: r.update(completed_at=(NOW + timedelta(seconds=1)).isoformat()),
        lambda r: r.update(completed_at="2026-10-08T00:00:00"),
        lambda r: r.update(completed_at="invalid"),
    ],
)
def test_incomplete_mismatched_or_expired_staging_evidence_fails(artifacts, tmp_path, mutation):
    _, candidate, previous = artifacts
    receipt = receipt_for(candidate, previous, tmp_path)
    value = read_json(receipt)
    mutation(value)
    receipt.write_text(json.dumps(value))
    with pytest.raises(ReleaseError):
        gate.release_gate(candidate, previous, receipt, PROGRAM_SHA, runner=lambda _: None, now=NOW)


def test_unsigned_or_wrong_signer_staging_evidence_fails(artifacts, tmp_path):
    _, candidate, previous = artifacts
    receipt = receipt_for(candidate, previous, tmp_path)

    def reject(command):
        if STAGING_IDENTITY in command:
            raise ReleaseError("forged staging signature")

    with pytest.raises(ReleaseError, match="forged"):
        gate.release_gate(candidate, previous, receipt, PROGRAM_SHA, runner=reject, now=NOW)
    receipt.with_suffix(".sigstore.json").unlink()
    with pytest.raises(ReleaseError, match="bounded regular file"):
        gate.release_gate(candidate, previous, receipt, PROGRAM_SHA, runner=lambda _: None, now=NOW)
