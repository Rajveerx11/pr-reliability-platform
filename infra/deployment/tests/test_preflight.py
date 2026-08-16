"""Deployment policy tests for private binding and external secrets."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from infra.deployment import preflight
from infra.deployment.preflight import PreflightError, validate_environment

IMAGE_KEYS = (
    "PLATFORM_IMAGE",
    "ACTIVITY_WORKER_IMAGE",
    "POSTGRES_IMAGE",
    "TEMPORAL_IMAGE",
    "CADDY_IMAGE",
    "OTEL_COLLECTOR_IMAGE",
    "PROMETHEUS_IMAGE",
)


def _deployment_files(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repository = tmp_path / "repository"
    repository.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    values = {
        "PRIVATE_BIND_ADDRESS": "10.20.30.40",
        "PRIVATE_HOSTNAME": "reviews.internal.example",
        "PRIVATE_BASE_URL": "https://reviews.internal.example",
        "DATABASE_URL": "postgresql://pr_reliability:database-secret@postgres:5432/pr_reliability",
        "OWNER_ID": "01J00000000000000000000001",
        "GITHUB_APP_ID": "1",
        "GITHUB_INSTALLATION_ID": "2",
        "GITHUB_WEBHOOK_SECRET": "w" * 32,
        "REVIEW_ACTIVITY_OPERATIONS_FACTORY": "provider:create",
        "MODEL_PROVIDER": "test",
        "BACKUP_DIRECTORY": str(tmp_path / "backups"),
        "DEPLOYMENT_SECRET_GID": str(os.getgid()) if os.name == "posix" else "2001",
        "POSTGRES_DB": "pr_reliability",
        "POSTGRES_USER": "pr_reliability",
        "SANDBOX_DOCKER_SOCKET": "/run/user/1001/docker.sock",
        "SANDBOX_ENGINE_UID": "1001",
        "SANDBOX_ENGINE_GID": "1001",
        "SANDBOX_STAGING_DIRECTORY": "/run/user/1001/pr-reliability-sandbox-staging",
    }
    for index, name in enumerate(IMAGE_KEYS):
        values[name] = f"registry.internal/image-{index}@sha256:{index + 1:064x}"
    for name, filename in (
        ("TLS_CERTIFICATE_FILE", "tls.crt"),
        ("TLS_PRIVATE_KEY_FILE", "tls.key"),
        ("TLS_CA_FILE", "ca.crt"),
        ("POSTGRES_PASSWORD_FILE", "postgres-password"),
        ("GITHUB_PRIVATE_KEY_FILE", "github.pem"),
    ):
        secret = secrets / filename
        content = "database-secret" if name == "POSTGRES_PASSWORD_FILE" else "secret"
        secret.write_text(content, encoding="utf-8")
        secret.chmod(0o640)
        values[name] = str(secret)
    environment_file = tmp_path / "deployment.env"
    environment_file.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
        encoding="utf-8",
    )
    environment_file.chmod(0o600)
    return repository, environment_file, values


def test_preflight_accepts_external_secrets_private_tls_and_digest_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment_file, values = _deployment_files(tmp_path)
    monkeypatch.setattr(preflight, "_validate_rootless_paths", lambda *_: None)

    assert validate_environment(repository, environment_file) == values


def test_every_image_in_shipped_environment_example_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = preflight.load_environment(Path(__file__).parents[1] / "deployment.env.example")
    repository, environment_file, valid_values = _deployment_files(tmp_path)
    monkeypatch.setattr(preflight, "_validate_rootless_paths", lambda *_: None)

    for name in IMAGE_KEYS:
        values = {**valid_values, name: example[name]}
        environment_file.write_text(
            "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(PreflightError, match="non-placeholder"):
            validate_environment(repository, environment_file)


@pytest.mark.parametrize("registry", ["registry.example", "example.invalid"])
def test_preflight_rejects_placeholder_registries_with_valid_looking_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: str,
) -> None:
    repository, environment_file, values = _deployment_files(tmp_path)
    monkeypatch.setattr(preflight, "_validate_rootless_paths", lambda *_: None)
    values["PLATFORM_IMAGE"] = f"{registry}/platform@sha256:{1:064x}"
    environment_file.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PreflightError, match="non-placeholder"):
        validate_environment(repository, environment_file)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PLATFORM_IMAGE", "example.invalid/platform:latest", "immutable sha256"),
        ("PRIVATE_BIND_ADDRESS", "0.0.0.0", "private IP"),
        ("PRIVATE_BASE_URL", "http://reviews.internal.example", "HTTPS origin"),
        (
            "DATABASE_URL",
            "postgresql://pr_reliability:wrong@postgres:5432/pr_reliability",
            "must match",
        ),
    ],
)
def test_preflight_rejects_public_or_mutable_deployment_input(
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, environment_file, values = _deployment_files(tmp_path)
    monkeypatch.setattr(preflight, "_validate_rootless_paths", lambda *_: None)
    values[name] = value
    environment_file.write_text(
        "\n".join(f"{key}={item}" for key, item in values.items()) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PreflightError, match=message):
        validate_environment(repository, environment_file)


def test_preflight_rejects_secret_inside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment_file, values = _deployment_files(tmp_path)
    monkeypatch.setattr(preflight, "_validate_rootless_paths", lambda *_: None)
    private_key = repository / "private.key"
    private_key.write_text("secret", encoding="utf-8")
    if os.name == "posix":
        private_key.chmod(0o600)
    values["TLS_PRIVATE_KEY_FILE"] = str(private_key)
    environment_file.write_text(
        "\n".join(f"{key}={item}" for key, item in values.items()) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PreflightError, match="outside the repository"):
        validate_environment(repository, environment_file)


@pytest.mark.skipif(os.name != "posix", reason="Unix socket ownership is POSIX-only")
def test_rootless_runtime_rejects_socket_symlink(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    staging = runtime / "pr-reliability-sandbox-staging"
    staging.mkdir(mode=0o770)
    socket_path = runtime / "engine.sock"
    with socket.socket(socket.AF_UNIX) as engine:
        engine.bind(str(socket_path))
        linked = runtime / "docker.sock"
        linked.symlink_to(socket_path)

        with pytest.raises(PreflightError, match="symlinks"):
            preflight._validate_rootless_paths(linked, staging, os.getuid(), os.getgid())


@pytest.mark.skipif(os.name != "posix", reason="Unix socket ownership is POSIX-only")
def test_rootless_runtime_requires_owned_socket_and_staging_directory(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    staging = runtime / "pr-reliability-sandbox-staging"
    staging.mkdir()
    staging.chmod(0o770)
    socket_path = runtime / "docker.sock"
    with socket.socket(socket.AF_UNIX) as engine:
        engine.bind(str(socket_path))
        socket_path.chmod(0o660)
        preflight._validate_rootless_paths(socket_path, staging, os.getuid(), os.getgid())

    socket_path.unlink()
    socket_path.write_text("not a socket", encoding="utf-8")
    with pytest.raises(PreflightError, match="Unix socket"):
        preflight._validate_rootless_paths(socket_path, staging, os.getuid(), os.getgid())


def test_vm_compose_exposes_only_private_tls_and_loopback_monitoring() -> None:
    compose = (Path(__file__).parents[1] / "compose.vm.yaml").read_text(encoding="utf-8")

    assert compose.count("    ports:\n") == 2
    assert "      - 127.0.0.1:9090:9090" in compose
    assert "      - ${PRIVATE_BIND_ADDRESS:?PRIVATE_BIND_ADDRESS is required}:443:443" in compose
    assert "  private:\n    internal: true" in compose
    assert (
        "      TMPDIR: ${SANDBOX_STAGING_DIRECTORY:?SANDBOX_STAGING_DIRECTORY is required}"
        in compose
    )
    assert compose.count("${SANDBOX_STAGING_DIRECTORY:?SANDBOX_STAGING_DIRECTORY is required}") >= 3
    assert (
        '    user: "${SANDBOX_ENGINE_UID:?SANDBOX_ENGINE_UID is required}:'
        '${SANDBOX_ENGINE_GID:?SANDBOX_ENGINE_GID is required}"' in compose
    )
    for secret in (
        "github_private_key",
        "postgres_password",
        "tls_certificate",
        "tls_private_key",
    ):
        assert f"  {secret}:\n" in compose


def test_backup_timer_is_persistent_and_uses_private_umask() -> None:
    deployment = Path(__file__).parents[1]
    service = (deployment / "pr-reliability-backup.service").read_text(encoding="utf-8")
    timer = (deployment / "pr-reliability-backup.timer").read_text(encoding="utf-8")

    assert "UMask=0077" in service
    assert "infra.deployment.database" in service
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=15m" in timer
