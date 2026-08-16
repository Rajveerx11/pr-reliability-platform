"""Fail-closed checks for external deployment configuration and secrets."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import stat
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

_DIGEST_IMAGE = re.compile(r"^[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$")
_IMAGE_KEYS = (
    "PLATFORM_IMAGE",
    "ACTIVITY_WORKER_IMAGE",
    "POSTGRES_IMAGE",
    "TEMPORAL_IMAGE",
    "CADDY_IMAGE",
    "OTEL_COLLECTOR_IMAGE",
    "PROMETHEUS_IMAGE",
)
_SECRET_FILE_KEYS = (
    "TLS_CERTIFICATE_FILE",
    "TLS_PRIVATE_KEY_FILE",
    "TLS_CA_FILE",
    "POSTGRES_PASSWORD_FILE",
    "GITHUB_PRIVATE_KEY_FILE",
)
_REQUIRED_VALUES = (
    "DATABASE_URL",
    "OWNER_ID",
    "GITHUB_APP_ID",
    "GITHUB_INSTALLATION_ID",
    "GITHUB_WEBHOOK_SECRET",
    "REVIEW_ACTIVITY_OPERATIONS_FACTORY",
    "MODEL_PROVIDER",
    "BACKUP_DIRECTORY",
    "DEPLOYMENT_SECRET_GID",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "SANDBOX_DOCKER_SOCKET",
    "SANDBOX_ENGINE_UID",
    "SANDBOX_ENGINE_GID",
    "SANDBOX_STAGING_DIRECTORY",
)


class PreflightError(RuntimeError):
    """Deployment inputs are unsafe or incomplete."""


def load_environment(path: Path) -> dict[str, str]:
    """Read one strict Docker-style environment file without evaluating shell code."""
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line or line.startswith("export "):
            raise PreflightError(f"invalid environment line {line_number}")
        name, value = line.split("=", 1)
        if not name or not name.replace("_", "").isalnum() or name in values:
            raise PreflightError(f"invalid environment name on line {line_number}")
        values[name] = value
    return values


def validate_environment(repository: Path, environment_file: Path) -> dict[str, str]:
    """Validate private binding, immutable images, and external secret files."""
    repository = repository.resolve(strict=True)
    environment_file = _external_regular_file(repository, environment_file, "deployment env")
    _require_private_mode(environment_file, 0o600)
    values = load_environment(environment_file)

    missing = [
        name
        for name in (*_IMAGE_KEYS, *_SECRET_FILE_KEYS, *_REQUIRED_VALUES)
        if not values.get(name)
    ]
    if missing:
        raise PreflightError(f"missing deployment values: {', '.join(missing)}")
    for name in _IMAGE_KEYS:
        if _DIGEST_IMAGE.fullmatch(values[name]) is None or values[name].startswith(
            "example.invalid/"
        ):
            raise PreflightError(f"{name} must use an immutable sha256 image digest")
    if len(values["GITHUB_WEBHOOK_SECRET"]) < 32 or values["GITHUB_WEBHOOK_SECRET"].startswith(
        "replace-"
    ):
        raise PreflightError("GITHUB_WEBHOOK_SECRET must be a non-example secret")
    if values["MODEL_PROVIDER"] == "openai" and (
        len(values.get("OPENAI_API_KEY", "")) < 20
        or values["OPENAI_API_KEY"].startswith("replace-")
    ):
        raise PreflightError("OPENAI_API_KEY must be configured outside source")

    try:
        bind_address = ipaddress.ip_address(values.get("PRIVATE_BIND_ADDRESS", ""))
    except ValueError as exc:
        raise PreflightError("PRIVATE_BIND_ADDRESS must be an IP address") from exc
    if not bind_address.is_private or bind_address.is_loopback or bind_address.is_unspecified:
        raise PreflightError("PRIVATE_BIND_ADDRESS must be a non-loopback private IP address")

    private_url = urlparse(values.get("PRIVATE_BASE_URL", ""))
    if (
        private_url.scheme != "https"
        or not private_url.hostname
        or private_url.path not in {"", "/"}
    ):
        raise PreflightError("PRIVATE_BASE_URL must be an HTTPS origin")
    if private_url.hostname != values.get("PRIVATE_HOSTNAME"):
        raise PreflightError("PRIVATE_BASE_URL must use PRIVATE_HOSTNAME")
    database_url = urlparse(values["DATABASE_URL"])
    if (
        database_url.scheme not in {"postgres", "postgresql"}
        or database_url.hostname != "postgres"
        or database_url.username != "pr_reliability"
        or database_url.path != "/pr_reliability"
        or not database_url.password
    ):
        raise PreflightError("DATABASE_URL must use the private Postgres service with credentials")
    if values["POSTGRES_DB"] != "pr_reliability" or values["POSTGRES_USER"] != "pr_reliability":
        raise PreflightError("Postgres database and user names must match the backup contract")
    if not values["DEPLOYMENT_SECRET_GID"].isdigit() or int(values["DEPLOYMENT_SECRET_GID"]) < 1:
        raise PreflightError("DEPLOYMENT_SECRET_GID must be positive")
    secret_group = int(values["DEPLOYMENT_SECRET_GID"])
    postgres_password: str | None = None

    for name in _SECRET_FILE_KEYS:
        secret = _external_regular_file(repository, Path(values[name]), name)
        if secret.stat().st_size == 0:
            raise PreflightError(f"{name} must not be empty")
        public_certificate = name in {"TLS_CERTIFICATE_FILE", "TLS_CA_FILE"}
        _require_private_mode(secret, 0o644 if public_certificate else 0o640)
        if os.name == "posix" and not public_certificate and secret.stat().st_mode & 0o777 != 0o640:
            raise PreflightError(f"{name} must use mode 0640")
        if os.name == "posix" and not public_certificate and secret.stat().st_gid != secret_group:
            raise PreflightError(f"{name} must belong to DEPLOYMENT_SECRET_GID")
        if name == "POSTGRES_PASSWORD_FILE":
            postgres_password = secret.read_text(encoding="utf-8").rstrip("\r\n")
    if unquote(database_url.password) != postgres_password:
        raise PreflightError("DATABASE_URL password must match POSTGRES_PASSWORD_FILE")
    backup_directory = Path(values["BACKUP_DIRECTORY"])
    if not backup_directory.is_absolute() or backup_directory.resolve().is_relative_to(repository):
        raise PreflightError("BACKUP_DIRECTORY must be an absolute path outside the repository")
    if not values["SANDBOX_ENGINE_UID"].isdigit() or int(values["SANDBOX_ENGINE_UID"]) < 1:
        raise PreflightError("SANDBOX_ENGINE_UID must be positive")
    if not values["SANDBOX_ENGINE_GID"].isdigit() or int(values["SANDBOX_ENGINE_GID"]) < 1:
        raise PreflightError("SANDBOX_ENGINE_GID must be positive")
    engine_uid = int(values["SANDBOX_ENGINE_UID"])
    engine_gid = int(values["SANDBOX_ENGINE_GID"])
    staging = PurePosixPath(values["SANDBOX_STAGING_DIRECTORY"])
    socket = PurePosixPath(values["SANDBOX_DOCKER_SOCKET"])
    if (
        not staging.is_absolute()
        or staging.name != "pr-reliability-sandbox-staging"
        or len(staging.parts) < 5
        or staging.parts[:3] != ("/", "run", "user")
        or staging.parts[3] != str(engine_uid)
        or socket.name != "docker.sock"
        or socket.parent != staging.parent
    ):
        raise PreflightError("sandbox paths must use one rootless runtime directory")
    if os.name == "posix":
        _validate_rootless_paths(
            Path(values["SANDBOX_DOCKER_SOCKET"]),
            Path(values["SANDBOX_STAGING_DIRECTORY"]),
            engine_uid,
            engine_gid,
        )
    return values


def _validate_rootless_paths(socket: Path, staging: Path, uid: int, gid: int) -> None:
    if socket.is_symlink() or staging.is_symlink():
        raise PreflightError("sandbox runtime paths must not be symlinks")
    try:
        socket_status = socket.stat()
        staging_status = staging.stat()
    except OSError as exc:
        raise PreflightError("sandbox runtime paths must exist") from exc
    if not stat.S_ISSOCK(socket_status.st_mode):
        raise PreflightError("SANDBOX_DOCKER_SOCKET must be a Unix socket")
    if socket_status.st_mode & 0o007:
        raise PreflightError("sandbox socket permissions are too broad")
    if not stat.S_ISDIR(staging_status.st_mode) or staging_status.st_mode & 0o777 != 0o770:
        raise PreflightError("sandbox staging directory must use mode 0770")
    if (socket_status.st_uid, socket_status.st_gid) != (uid, gid):
        raise PreflightError("sandbox socket owner must match rootless engine IDs")
    if (staging_status.st_uid, staging_status.st_gid) != (uid, gid):
        raise PreflightError("sandbox staging owner must match rootless engine IDs")
    if socket.resolve().parent != staging.resolve().parent:
        raise PreflightError("sandbox runtime paths must share one real directory")


def _external_regular_file(repository: Path, path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise PreflightError(f"{label} must use an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PreflightError(f"{label} does not exist") from exc
    if path.is_symlink() or not resolved.is_file() or resolved.is_relative_to(repository):
        raise PreflightError(f"{label} must be a regular file outside the repository")
    return resolved


def _require_private_mode(path: Path, maximum: int) -> None:
    if os.name == "posix" and path.stat().st_mode & 0o777 & ~maximum:
        raise PreflightError(f"{path} permissions are too broad")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment_file", type=Path)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    validate_environment(arguments.repository, arguments.environment_file)
    print("deployment preflight passed")


if __name__ == "__main__":
    main()
