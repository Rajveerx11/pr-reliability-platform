"""Signer, issuer, commit and immutable image checks are mandatory, never mocked successes in production."""

import json

import pytest

from infra.release import verify
from infra.release.manifest import OWN_IMAGES, ReleaseError, read_json


def test_verifier_requires_exact_identity_issuer_commit_and_sbom_for_every_image(artifacts):
    repository, candidate, _ = artifacts
    commands = []
    manifest = verify.verify_release(candidate, repository=repository, runner=commands.append)
    assert len(commands) == 7
    assert commands[0][:2] == ["cosign", "verify-blob"]
    for command in commands:
        assert command[command.index("--certificate-identity") + 1] == verify.BUILD_IDENTITY
        assert command[command.index("--certificate-oidc-issuer") + 1] == verify.ISSUER
        assert command[command.index("--certificate-github-workflow-sha") + 1] == manifest["commit"]
        assert "--insecure-ignore-tlog" not in command
    for name in OWN_IMAGES:
        assert any(c[1] == "verify" and c[-1] == manifest["images"][name] for c in commands)
        assert any(
            c[1] == "verify-attestation" and c[-1] == manifest["images"][name] for c in commands
        )


@pytest.mark.parametrize("failed_command", ["verify-blob", "verify", "verify-attestation"])
def test_forged_signatures_or_missing_sbom_attestations_fail_closed(artifacts, failed_command):
    _, candidate, _ = artifacts

    def reject(command):
        if command[1] == failed_command:
            raise ReleaseError("invalid signature")

    with pytest.raises(ReleaseError, match="invalid signature"):
        verify.verify_release(candidate, runner=reject)


def test_unsigned_manifest_fails_even_with_complete_scan_files(artifacts):
    _, candidate, _ = artifacts
    (candidate / "release.sigstore.json").unlink()
    commands = []
    with pytest.raises(ReleaseError, match="bounded regular file"):
        verify.verify_release(candidate, runner=commands.append)
    assert commands == []


@pytest.mark.parametrize("change", ["migrations", "configuration"])
def test_checkout_mismatch_fails_before_image_verification(artifacts, change):
    repository, candidate, _ = artifacts
    path = repository / (
        "migrations/0001_initial.sql" if change == "migrations" else "infra/deployment/Caddyfile"
    )
    path.write_text("changed")
    commands = []
    with pytest.raises(ReleaseError, match="checkout"):
        verify.verify_release(candidate, repository=repository, runner=commands.append)
    assert len(commands) == 1


def deployment_values(candidate):
    manifest = read_json(candidate / "release.json")
    return {
        **manifest["images"],
        "RELEASE_DIRECTORY": str(candidate),
        "RELEASE_COMMIT": manifest["commit"],
        "DEPLOYMENT_CONFIG_VERSION": manifest["config_version"],
        "REVIEW_CHECK_ALLOWLIST_JSON": json.dumps(
            [
                {
                    "name": "tests",
                    "image": manifest["images"]["SANDBOX_IMAGE"],
                    "command": ["python", "-V"],
                }
            ]
        ),
    }


def test_preflight_consumes_all_signed_images(artifacts, monkeypatch):
    calls = []
    monkeypatch.setattr(verify, "validate_rendered_deployment", lambda *args: calls.append(args))
    repository, candidate, _ = artifacts
    assert (
        verify.validate_deployment_release(
            repository, deployment_values(candidate), runner=lambda _: None
        )["commit"]
        == "2" * 40
    )

    assert len(calls) == 1


@pytest.mark.parametrize(
    "field",
    [
        "RELEASE_COMMIT",
        "DEPLOYMENT_CONFIG_VERSION",
        "PLATFORM_IMAGE",
        "ACTIVITY_WORKER_IMAGE",
        "POSTGRES_IMAGE",
    ],
)
def test_deployment_mismatch_fails(artifacts, field):
    repository, candidate, _ = artifacts
    values = deployment_values(candidate)
    values[field] = "mismatched"
    with pytest.raises(ReleaseError, match="does not match"):
        verify.validate_deployment_release(repository, values, runner=lambda _: None)


def test_mismatched_check_tool_image_fails(artifacts):
    repository, candidate, _ = artifacts
    values = deployment_values(candidate)
    values["REVIEW_CHECK_ALLOWLIST_JSON"] = json.dumps(
        [
            {
                "name": "tests",
                "image": "ghcr.io/another/check@sha256:" + f"{91:064x}",
                "command": ["python", "-V"],
            }
        ]
    )
    with pytest.raises(ReleaseError, match="sandbox image"):
        verify.validate_deployment_release(repository, values, runner=lambda _: None)


def test_manifest_mutation_during_verification_fails(artifacts):
    _, candidate, _ = artifacts

    def mutate(_):
        (candidate / "release.json").write_text("{}")

    with pytest.raises(ReleaseError, match="changed during"):
        verify.verify_release(candidate, runner=mutate)


def test_missing_tool_and_nonzero_tool_do_not_expose_output(monkeypatch):
    import subprocess

    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, b"private stdout", b"private stderr"),
    )
    with pytest.raises(ReleaseError) as error:
        verify.run_checked(["cosign", "verify"])
    assert "private" not in str(error.value)


def test_blob_verification_uses_private_exact_byte_snapshot(artifacts):
    from pathlib import Path

    _, candidate, _ = artifacts
    path = candidate / "release.json"
    original = path.read_bytes()

    def inspect_snapshot(command):
        if command[1] == "verify-blob":
            document = Path(command[-1])
            assert document != path
            path.write_text("swapped while verifier runs")
            assert document.read_bytes() == original
            assert Path(command[command.index("--bundle") + 1]).read_bytes() == b"{}"
            path.write_bytes(original)

    verify.verify_release(candidate, runner=inspect_snapshot)


def test_swapped_blob_before_verification_never_reaches_tool(artifacts):
    _, candidate, _ = artifacts
    path = candidate / "release.json"
    commands = []
    with pytest.raises(ReleaseError, match="changed before"):
        verify.verify_blob(
            path,
            candidate / "release.sigstore.json",
            verify.BUILD_IDENTITY,
            "2" * 40,
            expected_sha256="f" * 64,
            runner=commands.append,
        )
    assert commands == []


@pytest.mark.parametrize("failure", [OSError("private failure"), TimeoutError("private failure")])
def test_unavailable_tool_fails_without_output(monkeypatch, failure):
    # TimeoutExpired is the specific subprocess failure handled by the production runner.
    import subprocess

    if isinstance(failure, TimeoutError):
        failure = subprocess.TimeoutExpired("cosign", 600, output="private stdout")

    def reject(*_, **__):
        raise failure

    monkeypatch.setattr(verify.subprocess, "run", reject)
    with pytest.raises(ReleaseError) as error:
        verify.run_checked(["cosign", "verify"])
    assert "private" not in str(error.value)


@pytest.mark.parametrize("target", ["manifest", "evidence", "bundle"])
def test_final_image_command_cannot_swap_authenticated_artifacts(artifacts, target):
    _, candidate, _ = artifacts
    commands = []

    def late_swap(command):
        commands.append(command)
        if len(commands) == 7:
            path = (
                candidate
                / {
                    "manifest": "release.json",
                    "evidence": "POSTGRES_IMAGE.sbom.json",
                    "bundle": "release.sigstore.json",
                }[target]
            )
            path.write_text("replaced after last image command")

    with pytest.raises(ReleaseError, match="changed during"):
        verify.verify_release(candidate, runner=late_swap)
    assert len(commands) == 7


def test_combined_evidence_and_operations_environment_passes_release_preflight(
    artifacts, tmp_path, monkeypatch
):
    from infra.deployment import preflight
    from infra.deployment.tests.test_preflight import _deployment_files

    repository, candidate, _ = artifacts
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    _, environment, values = _deployment_files(deployment)
    values.update(deployment_values(candidate))
    environment.write_text("
".join(f"{key}={value}" for key, value in values.items()) + "
")
    monkeypatch.setattr(preflight, "_validate_rootless_paths", lambda *_: None)
    real_validate = verify.validate_deployment_release
    # Only external Cosign/Compose are synthetic. Manifest, migrations, configuration,
    # evidence settings and all remaining preflight checks execute normally.
    monkeypatch.setattr(
        verify,
        "validate_deployment_release",
        lambda repo, env: real_validate(repo, env, runner=lambda _: None),
    )
    rendered = []
    monkeypatch.setattr(verify, "validate_rendered_deployment", lambda *args: rendered.append(args))
    assert preflight.validate_environment(repository, environment) == values
    assert len(rendered) == 1
    manifest = rendered[0][2]
    assert "0009_verification_artifacts.sql" in manifest["migration_set"]
    assert "0010_runner_operations.sql" in manifest["migration_set"]
    compose = (repository / "infra/deployment/compose.vm.yaml").read_text()
    for setting in ("EVIDENCE_ENCRYPTION_KEY", "RUNNER_CAPACITY", "OPERATIONS_BACKUP_RECEIPT_DIRECTORY"):
        assert setting in compose


@pytest.mark.parametrize("migration", ["0009_verification_artifacts.sql", "0010_runner_operations.sql"])
def test_either_feature_migration_drift_blocks_release(artifacts, migration):
    repository, candidate, _ = artifacts
    with (repository / "migrations" / migration).open("a") as file:
        file.write("-- changed fixture
")
    with pytest.raises(ReleaseError, match="checkout"):
        verify.verify_release(candidate, repository=repository, runner=lambda _: None)
