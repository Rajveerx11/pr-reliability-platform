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
    with pytest.raises(ReleaseError, match="bundle"):
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


def test_preflight_consumes_all_signed_images(artifacts):
    repository, candidate, _ = artifacts
    assert (
        verify.validate_deployment_release(
            repository, deployment_values(candidate), runner=lambda _: None
        )["commit"]
        == "2" * 40
    )


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
