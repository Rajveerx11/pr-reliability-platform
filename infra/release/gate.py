"""Authenticate complete staging evidence before preparing a GitHub release."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from .manifest import SHA256, ReleaseError, digest, read_document, require_compatible
from .staging import CHECKS
from .verify import STAGING_IDENTITY, Runner, run_checked, verify_blob, verify_release


def release_gate(
    candidate_directory: Path,
    previous_directory: Path,
    receipt_path: Path,
    e2e_program_sha256: str,
    *,
    runner: Runner = run_checked,
    now: datetime | None = None,
) -> dict:
    candidate = verify_release(candidate_directory, runner=runner)
    previous = verify_release(previous_directory, runner=runner)
    candidate_sha = digest(candidate_directory / "release.json")
    previous_sha = digest(previous_directory / "release.json")
    require_compatible(candidate, previous, previous_sha)
    receipt, before = read_document(receipt_path)
    verify_blob(
        receipt_path,
        receipt_path.with_suffix(".sigstore.json"),
        STAGING_IDENTITY,
        candidate["commit"],
        expected_sha256=before,
        runner=runner,
    )
    if digest(receipt_path) != before:
        raise ReleaseError("staging evidence changed during verification")
    expected = {
        "schema_version",
        "environment",
        "commit",
        "manifest_sha256",
        "previous_manifest_sha256",
        "run_id",
        "completed_at",
        "checks",
        "backup_manifest_sha256",
        "e2e_program_sha256",
    }
    if (
        set(receipt) != expected
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["environment"] != "linux-staging"
        or receipt["commit"] != candidate["commit"]
        or receipt["manifest_sha256"] != candidate_sha
        or receipt["previous_manifest_sha256"] != previous_sha
        or not isinstance(receipt["run_id"], str)
        or not receipt["run_id"].isdigit()
        or int(receipt["run_id"]) < 1
        or not SHA256.fullmatch(e2e_program_sha256)
        or receipt["e2e_program_sha256"] != e2e_program_sha256
        or not isinstance(receipt["backup_manifest_sha256"], str)
        or not SHA256.fullmatch(receipt["backup_manifest_sha256"])
        or receipt["checks"] != dict.fromkeys(CHECKS, "passed")
    ):
        raise ReleaseError("staging acceptance evidence is incomplete or mismatched")
    try:
        completed = datetime.fromisoformat(receipt["completed_at"])
        current = now or datetime.now(UTC)
        if completed.tzinfo is None or not timedelta(0) <= current - completed <= timedelta(
            hours=48
        ):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ReleaseError("staging evidence is expired or has an invalid completion time") from exc
    return candidate
