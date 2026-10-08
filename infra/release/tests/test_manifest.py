"""Content binding, strict schema, scanners and migration-safe rollback."""

import json

import pytest

from infra.release.manifest import (
    ReleaseError,
    digest,
    read_json,
    require_compatible,
    validate_evidence,
    validate_manifest,
)


def test_valid_content_and_rollback(artifacts):
    _, candidate, previous = artifacts
    manifest = read_json(candidate / "release.json")
    validate_manifest(manifest)
    validate_evidence(candidate, manifest)
    require_compatible(
        manifest, read_json(previous / "release.json"), digest(previous / "release.json")
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m.update(schema_version=True),
        lambda m: m.update(secret="should never be serialized"),
        lambda m: m.update(commit="main"),
        lambda m: m.update(config_version="unversioned"),
        lambda m: m.update(migration_set={}),
        lambda m: m["migration_set"].update({"../evil.sql": "a" * 64}),
        lambda m: m["images"].update(PLATFORM_IMAGE="ghcr.io/test/platform:latest"),
        lambda m: m["images"].update(
            PLATFORM_IMAGE="example.invalid/platform@sha256:" + f"{1:064x}"
        ),
        lambda m: m["images"].update(SANDBOX_IMAGE=m["images"]["PLATFORM_IMAGE"]),
        lambda m: m.update(rollback={"commit": "main", "manifest_sha256": "a" * 64}),
        lambda m: m["evidence"].pop("PLATFORM_IMAGE.sbom.json"),
        lambda m: m["evidence"].update({"../report.json": "a" * 64}),
    ],
)
def test_rejects_invalid_manifest(artifacts, mutation):
    _, candidate, _ = artifacts
    manifest = read_json(candidate / "release.json")
    mutation(manifest)
    with pytest.raises(ReleaseError):
        validate_manifest(manifest)


@pytest.mark.parametrize("kind", ["scan", "sbom"])
def test_missing_or_modified_evidence_fails(artifacts, kind):
    _, candidate, _ = artifacts
    manifest = read_json(candidate / "release.json")
    report = candidate / f"PLATFORM_IMAGE.{kind}.json"
    report.write_text("{}")
    with pytest.raises(ReleaseError):
        validate_evidence(candidate, manifest)
    report.unlink()
    with pytest.raises(ReleaseError):
        validate_evidence(candidate, manifest)


@pytest.mark.parametrize("severity", ["HIGH", "CRITICAL", "UNKNOWN", None])
def test_unresolved_or_unknown_vulnerabilities_fail(artifacts, severity):
    _, candidate, _ = artifacts
    manifest = read_json(candidate / "release.json")
    path = candidate / "PLATFORM_IMAGE.scan.json"
    report = read_json(path)
    report["Results"][0]["Vulnerabilities"] = [{"Severity": severity}]
    path.write_text(json.dumps(report))
    manifest["evidence"][path.name] = digest(path)
    with pytest.raises(ReleaseError, match="unresolved"):
        validate_evidence(candidate, manifest)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(ArtifactName="ghcr.io/test/platform:mutable"),
        lambda r: r.update(Results=[]),
        lambda r: r.update(Results=["invalid"]),
        lambda r: r.update(ArtifactType="repository"),
        lambda r: r["Results"][0].update(Vulnerabilities={"Severity": "LOW"}),
    ],
)
def test_absent_or_wrong_image_scans_fail(artifacts, mutation):
    _, candidate, _ = artifacts
    manifest = read_json(candidate / "release.json")
    path = candidate / "PLATFORM_IMAGE.scan.json"
    report = read_json(path)
    mutation(report)
    path.write_text(json.dumps(report))
    manifest["evidence"][path.name] = digest(path)
    with pytest.raises(ReleaseError):
        validate_evidence(candidate, manifest)


@pytest.mark.parametrize("field", ["migration_set", "config_version", "rollback"])
def test_rollback_rejects_schema_config_or_target_mismatch(artifacts, field):
    _, candidate, previous = artifacts
    manifest = read_json(candidate / "release.json")
    manifest[field] = {} if field != "config_version" else "a" * 64
    with pytest.raises(ReleaseError):
        require_compatible(
            manifest, read_json(previous / "release.json"), digest(previous / "release.json")
        )


def test_duplicate_json_keys_and_symlinks_rejected(tmp_path):
    path = tmp_path / "release.json"
    path.write_text('{"commit":"approved","commit":"forged"}')
    with pytest.raises(ReleaseError):
        read_json(path)
    link = tmp_path / "link.json"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("host does not allow creating symlinks")
    with pytest.raises(ReleaseError):
        read_json(link)
