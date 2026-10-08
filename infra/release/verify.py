"""Verify Sigstore identities and release content before any deployment command."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from .manifest import (
    OWN_IMAGES,
    ReleaseError,
    config_version,
    digest,
    migration_set,
    read_bytes,
    read_document,
    validate_evidence,
    validate_manifest,
)

REPOSITORY = "Rajveerx11/pr-reliability-platform"
ISSUER = "https://token.actions.githubusercontent.com"
BUILD_IDENTITY = (
    f"https://github.com/{REPOSITORY}/.github/workflows/release-build.yml@refs/heads/main"
)
STAGING_IDENTITY = (
    f"https://github.com/{REPOSITORY}/.github/workflows/release-staging.yml@refs/heads/main"
)
Runner = Callable[[Sequence[str]], None]


def run_checked(command: Sequence[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(
            "required release verification tool is unavailable or timed out"
        ) from exc
    if result.returncode != 0:
        # Do not relay tool output: runtime credentials and server responses may be sensitive.
        raise ReleaseError("release command failed; no promotion evidence was produced")


def identity_flags(identity: str, commit: str) -> list[str]:
    return [
        "--certificate-identity",
        identity,
        "--certificate-oidc-issuer",
        ISSUER,
        "--certificate-github-workflow-sha",
        commit,
        "--certificate-github-workflow-repository",
        REPOSITORY,
        "--certificate-github-workflow-ref",
        "refs/heads/main",
        "--certificate-github-workflow-trigger",
        "workflow_dispatch",
    ]


def verify_blob(
    path: Path,
    bundle: Path,
    identity: str,
    commit: str,
    *,
    expected_sha256: str,
    runner: Runner = run_checked,
) -> None:
    if path.is_symlink() or bundle.is_symlink() or not path.is_file() or not bundle.is_file():
        raise ReleaseError("signed release document and bundle are required")
    content, signature = read_bytes(path), read_bytes(bundle)
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ReleaseError("signed document changed before signature verification")
    # Verify the exact parsed bytes from a private snapshot. Checking only the source
    # before/after a subprocess would permit a file swap during verification.
    with tempfile.TemporaryDirectory() as temporary:
        document = Path(temporary) / "document.json"
        snapshot_bundle = Path(temporary) / "bundle.json"
        document.write_bytes(content)
        snapshot_bundle.write_bytes(signature)
        runner(
            [
                "cosign",
                "verify-blob",
                "--bundle",
                str(snapshot_bundle),
                *identity_flags(identity, commit),
                str(document),
            ]
        )


def verify_release(
    directory: Path, *, repository: Path | None = None, runner: Runner = run_checked
) -> dict:
    path = directory / "release.json"
    manifest, before = read_document(path)
    validate_manifest(manifest)
    verify_blob(
        path,
        directory / "release.sigstore.json",
        BUILD_IDENTITY,
        manifest["commit"],
        expected_sha256=before,
        runner=runner,
    )
    if digest(path) != before:
        raise ReleaseError("release changed during signature verification")
    validate_evidence(directory, manifest)
    if repository is not None and (
        migration_set(repository) != manifest["migration_set"]
        or config_version(repository) != manifest["config_version"]
    ):
        raise ReleaseError("checkout migrations or deployment configuration do not match release")
    for name in OWN_IMAGES:
        image = manifest["images"][name]
        flags = identity_flags(BUILD_IDENTITY, manifest["commit"])
        runner(["cosign", "verify", *flags, image])
        runner(["cosign", "verify-attestation", "--type", "cyclonedx", *flags, image])
    return manifest


def validate_deployment_release(
    repository: Path, values: dict[str, str], *, runner: Runner = run_checked
) -> dict:
    from pr_reliability_workers.sandbox import parse_check_allowlist

    directory = Path(values.get("RELEASE_DIRECTORY", ""))
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise ReleaseError("RELEASE_DIRECTORY must be an absolute signed artifact directory")
    manifest = verify_release(directory, repository=repository, runner=runner)
    if values.get("RELEASE_COMMIT") != manifest["commit"]:
        raise ReleaseError("deployment commit does not match signed release")
    if values.get("DEPLOYMENT_CONFIG_VERSION") != manifest["config_version"]:
        raise ReleaseError("deployment configuration version does not match signed release")
    for name, image in manifest["images"].items():
        if name != "SANDBOX_IMAGE" and values.get(name) != image:
            raise ReleaseError("deployment image does not match signed release")
    checks = parse_check_allowlist(values["REVIEW_CHECK_ALLOWLIST_JSON"])
    if any(check.image != manifest["images"]["SANDBOX_IMAGE"] for check in checks.checks):
        raise ReleaseError("check image does not match the signed sandbox image")
    return manifest
