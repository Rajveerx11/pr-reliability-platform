"""Strict, content-bound release manifests. No environment secrets are serialized."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from infra.deployment.preflight import _IMAGE_KEYS, PreflightError, _require_release_image

OWN_IMAGES = ("PLATFORM_IMAGE", "ACTIVITY_WORKER_IMAGE", "SANDBOX_IMAGE")
IMAGE_KEYS = (*_IMAGE_KEYS, "SANDBOX_IMAGE")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
CONFIG_FILES = (
    "infra/deployment/compose.vm.yaml",
    "infra/deployment/Caddyfile",
    "infra/deployment/activity-entrypoint.sh",
    "infra/deployment/postgres-init.sh",
    "infra/deployment/temporal-entrypoint.sh",
    "infra/deployment/prometheus.yaml",
    "infra/deployment/rules.yaml",
    "infra/observability/collector.yaml",
)


class ReleaseError(RuntimeError):
    """A release or its evidence is absent, untrusted, or incompatible."""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise ReleaseError("release evidence must be a bounded regular file")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise ReleaseError("release evidence is missing or invalid JSON") from exc


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def write_json(path: Path, value: dict) -> None:
    # Exclusive creation prevents accidentally replacing an already approved artifact.
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, sort_keys=True, indent=2) + "\n")


def migration_set(repository: Path) -> dict[str, str]:
    return {path.name: digest(path) for path in sorted((repository / "migrations").glob("*.sql"))}


def config_version(repository: Path) -> str:
    files = {name: digest(repository / name) for name in CONFIG_FILES}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def validate_images(images: dict) -> None:
    if not isinstance(images, dict) or set(images) != set(IMAGE_KEYS):
        raise ReleaseError("release must record every deployment image and the sandbox")
    try:
        for name, image in images.items():
            if not isinstance(image, str):
                raise ReleaseError("release image must be a string")
            _require_release_image(name, image)
    except PreflightError as exc:
        raise ReleaseError("release contains an invalid immutable image") from exc
    if len({images[name] for name in OWN_IMAGES}) != 3:
        raise ReleaseError("platform, provider-activity, and sandbox images must be separate")


def validate_manifest(value: dict) -> None:
    expected = {
        "schema_version",
        "commit",
        "images",
        "migration_set",
        "config_version",
        "rollback",
        "evidence",
    }
    if (
        set(value) != expected
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ReleaseError("unsupported release manifest")
    if not isinstance(value["commit"], str) or not COMMIT.fullmatch(value["commit"]):
        raise ReleaseError("release commit must be a full Git SHA")
    validate_images(value["images"])
    migrations = value["migration_set"]
    if (
        not isinstance(migrations, dict)
        or not migrations
        or any(
            not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", name)
            or not isinstance(checksum, str)
            or not SHA256.fullmatch(checksum)
            for name, checksum in migrations.items()
        )
        or not isinstance(value["config_version"], str)
        or not SHA256.fullmatch(value["config_version"])
    ):
        raise ReleaseError("release migration set or configuration version is invalid")
    rollback = value["rollback"]
    if rollback is not None and (
        not isinstance(rollback, dict)
        or set(rollback) != {"manifest_sha256", "commit"}
        or not isinstance(rollback["commit"], str)
        or not COMMIT.fullmatch(rollback["commit"])
        or not isinstance(rollback["manifest_sha256"], str)
        or not SHA256.fullmatch(rollback["manifest_sha256"])
    ):
        raise ReleaseError("rollback target is invalid")
    expected_evidence = {f"{name}.{kind}.json" for name in OWN_IMAGES for kind in ("scan", "sbom")}
    evidence = value["evidence"]
    if (
        not isinstance(evidence, dict)
        or set(evidence) != expected_evidence
        or any(
            not isinstance(checksum, str) or not SHA256.fullmatch(checksum)
            for checksum in evidence.values()
        )
    ):
        raise ReleaseError("every built image requires a scan and SBOM")


def validate_evidence(directory: Path, manifest: dict) -> None:
    for name, checksum in manifest["evidence"].items():
        report = read_json(directory / name)
        if digest(directory / name) != checksum:
            raise ReleaseError("release evidence checksum mismatch")
        if name.endswith(".sbom.json"):
            if report.get("bomFormat") != "CycloneDX" or not isinstance(
                report.get("components"), list
            ):
                raise ReleaseError("image SBOM is invalid")
        else:
            image_key = name.removesuffix(".scan.json")
            if report.get("ArtifactName") != manifest["images"][image_key]:
                raise ReleaseError("scan does not name the immutable image")
            if (
                report.get("SchemaVersion") != 2
                or report.get("ArtifactType") != "container_image"
                or not isinstance(report.get("Results"), list)
                or not report["Results"]
            ):
                raise ReleaseError("image scan is incomplete")
            for result in report["Results"]:
                if not isinstance(result, dict):
                    raise ReleaseError("image scan result is invalid")
                vulnerabilities = result.get("Vulnerabilities") or []
                if not isinstance(vulnerabilities, list):
                    raise ReleaseError("image vulnerabilities are invalid")
                for vulnerability in vulnerabilities:
                    if not isinstance(vulnerability, dict) or vulnerability.get("Severity") not in {
                        "LOW",
                        "MEDIUM",
                    }:
                        raise ReleaseError("image scan contains unresolved or unknown severity")


def create_manifest(
    repository: Path,
    commit: str,
    images: dict,
    directory: Path,
    previous: dict | None = None,
    previous_sha: str | None = None,
) -> dict:
    value = {
        "schema_version": 1,
        "commit": commit,
        "images": images,
        "migration_set": migration_set(repository),
        "config_version": config_version(repository),
        "rollback": None
        if previous is None
        else {"commit": previous["commit"], "manifest_sha256": previous_sha},
        "evidence": {
            f"{name}.{kind}.json": digest(directory / f"{name}.{kind}.json")
            for name in OWN_IMAGES
            for kind in ("scan", "sbom")
        },
    }
    validate_manifest(value)
    validate_evidence(directory, value)
    if previous is not None:
        require_compatible(value, previous, previous_sha)
    return value


def require_compatible(candidate: dict, previous: dict, previous_sha: str | None) -> None:
    validate_manifest(previous)
    if candidate["rollback"] != {"commit": previous["commit"], "manifest_sha256": previous_sha}:
        raise ReleaseError("previous release does not match the approved rollback target")
    # No automatic downgrade or down-migration. A changed schema needs a separate reviewed plan.
    if (
        candidate["migration_set"] != previous["migration_set"]
        or candidate["config_version"] != previous["config_version"]
    ):
        raise ReleaseError("rollback requires identical migration sets and configuration versions")
    if candidate["commit"] == previous["commit"]:
        raise ReleaseError("rollback target must be a previous commit")
