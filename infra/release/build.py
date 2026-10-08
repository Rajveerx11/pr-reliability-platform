"""Build from a clean approved main commit; scan before push and sign only passing artifacts."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .manifest import (
    OWN_IMAGES,
    ReleaseError,
    config_version,
    create_manifest,
    digest,
    migration_set,
    read_json,
    require_compatible,
    validate_images,
    write_json,
)
from .verify import REPOSITORY, run_checked, verify_release


def output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=1200)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError("release tool is unavailable or timed out") from exc
    if result.returncode:
        raise ReleaseError("release build command failed")
    return result.stdout.strip()


def build(
    repository: Path,
    commit: str,
    upstream_file: Path,
    directory: Path,
    previous_directory: Path | None = None,
) -> dict:
    # The workflow additionally checks approved-main Quality before granting registry/OIDC access.
    if output(["git", "rev-parse", "HEAD"]) != commit or output(
        ["git", "status", "--porcelain", "--untracked-files=all"]
    ):
        raise ReleaseError("release build requires a clean exact-commit checkout")
    upstream = read_json(upstream_file)
    images = {
        **upstream,
        **{
            name: f"ghcr.io/{REPOSITORY.lower()}/{suffix}@sha256:{index + 1:064x}"
            for index, (name, suffix) in enumerate(
                zip(OWN_IMAGES, ("platform", "provider-activity", "sandbox"), strict=True)
            )
        },
    }
    validate_images(images)
    if set(upstream) != set(images) - set(OWN_IMAGES):
        raise ReleaseError("upstream input must contain only third-party deployment images")
    previous = None if previous_directory is None else verify_release(previous_directory)
    if previous is not None:
        require_compatible(
            {
                "commit": commit,
                "migration_set": migration_set(repository),
                "config_version": config_version(repository),
                "rollback": {
                    "commit": previous["commit"],
                    "manifest_sha256": digest(previous_directory / "release.json"),
                },
            },
            previous,
            digest(previous_directory / "release.json"),
        )
    directory.mkdir(parents=True, exist_ok=False)
    # Git archive is the only build context. Local files, credentials, and the runner's .git
    # cannot enter Docker, even if a later Dockerfile COPY is broadened accidentally.
    with tempfile.TemporaryDirectory() as temporary:
        context = Path(temporary) / "context"
        context.mkdir()
        archive = Path(temporary) / "source.tar"
        output(["git", "archive", "--format=tar", f"--output={archive}", commit])
        output(["tar", "--extract", "--file", str(archive), "--directory", str(context)])
        for name, suffix, dockerfile in zip(
            OWN_IMAGES,
            ("platform", "provider-activity", "sandbox"),
            (
                "infra/compose/Dockerfile",
                "infra/release/Dockerfile.activity",
                "infra/release/Dockerfile.sandbox",
            ),
            strict=True,
        ):
            tag = f"ghcr.io/{REPOSITORY.lower()}/{suffix}:{commit}"
            args = (
                ["--build-arg", f"PLATFORM_IMAGE={images['PLATFORM_IMAGE']}"]
                if name == "ACTIVITY_WORKER_IMAGE"
                else []
            )
            output(
                [
                    "docker",
                    "build",
                    "--platform=linux/amd64",
                    "--file",
                    str(context / dockerfile),
                    "--tag",
                    tag,
                    *args,
                    str(context),
                ]
            )
            output(["trivy", "image", "--scanners", "secret", "--exit-code", "1", tag])
            output(
                [
                    "trivy",
                    "image",
                    "--scanners",
                    "vuln",
                    "--exit-code",
                    "1",
                    "--severity",
                    "UNKNOWN,HIGH,CRITICAL",
                    tag,
                ]
            )
            output(["docker", "push", tag])
            images[name] = output(
                ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", tag]
            )
            validate_images(images)
            output(
                [
                    "trivy",
                    "image",
                    "--scanners",
                    "vuln",
                    "--format",
                    "json",
                    "--output",
                    str(directory / f"{name}.scan.json"),
                    images[name],
                ]
            )
            output(
                [
                    "trivy",
                    "image",
                    "--format",
                    "cyclonedx",
                    "--output",
                    str(directory / f"{name}.sbom.json"),
                    images[name],
                ]
            )
    value = create_manifest(
        repository,
        commit,
        images,
        directory,
        previous,
        None if previous is None else digest(previous_directory / "release.json"),
    )
    write_json(directory / "release.json", value)
    for name in OWN_IMAGES:
        run_checked(["cosign", "sign", "--yes", images[name]])
        run_checked(
            [
                "cosign",
                "attest",
                "--yes",
                "--type",
                "cyclonedx",
                "--predicate",
                str(directory / f"{name}.sbom.json"),
                images[name],
            ]
        )
    run_checked(
        [
            "cosign",
            "sign-blob",
            "--yes",
            "--bundle",
            str(directory / "release.sigstore.json"),
            str(directory / "release.json"),
        ]
    )
    verify_release(directory, repository=repository)
    return value
