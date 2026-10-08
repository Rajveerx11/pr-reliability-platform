"""Run staging tools without ambient deployment overrides; check rendered identities."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

from .manifest import ReleaseError

SERVICE_IMAGES = {
    **dict.fromkeys(
        ("migrate", "api", "repository-sync", "command-dispatcher", "workflow-worker"),
        "PLATFORM_IMAGE",
    ),
    "activity-worker": "ACTIVITY_WORKER_IMAGE",
    "postgres": "POSTGRES_IMAGE",
    "temporal": "TEMPORAL_IMAGE",
    "caddy": "CADDY_IMAGE",
    "otel-collector": "OTEL_COLLECTOR_IMAGE",
    "prometheus": "PROMETHEUS_IMAGE",
}


def controlled_environment(values: dict[str, str]) -> dict[str, str]:
    # Preserve only process essentials, not COMPOSE_*, DOCKER_HOST or deployment inputs.
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "WINDIR",
            "TMP",
            "TEMP",
            "LANG",
            "LC_ALL",
        )
        if key in os.environ
    }
    environment.update(values)
    return environment


def execute(command: Sequence[str], values: dict[str, str], *, stdin=None, stdout=None, text=False):
    try:
        return subprocess.run(
            command,
            env=controlled_environment(values),
            stdin=stdin,
            stdout=stdout or subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError("staging tool is unavailable or timed out") from exc


def database_runner(values: dict[str, str]):
    def run(command: Sequence[str], stdin: BinaryIO | None, stdout: BinaryIO | None) -> int:
        return execute(command, values, stdin=stdin, stdout=stdout).returncode

    return run


def checked(command: Sequence[str], values: dict[str, str]) -> None:
    if execute(command, values).returncode:
        raise ReleaseError("staging command failed; no promotion evidence was produced")


def validate_rendered_deployment(repository: Path, values: dict[str, str], manifest: dict) -> None:
    from pr_reliability_workers.sandbox import parse_check_allowlist

    # Explicit process values have precedence over dotenv interpolation. No ambient .env.
    with tempfile.TemporaryDirectory() as temporary:
        empty_env = Path(temporary) / "empty.env"
        empty_env.touch()
        result = execute(
            [
                "docker",
                "compose",
                "--env-file",
                str(empty_env),
                "--file",
                str(repository / "infra/deployment/compose.vm.yaml"),
                "config",
                "--format",
                "json",
            ],
            values,
        )
    if result.returncode:
        raise ReleaseError("cannot verify rendered staging configuration")
    try:
        services = json.loads(result.stdout)["services"]
        if set(services) != set(SERVICE_IMAGES):
            raise ValueError
        for service, key in SERVICE_IMAGES.items():
            if services[service]["image"] != manifest["images"][key]:
                raise ValueError
        checks = parse_check_allowlist(
            services["activity-worker"]["environment"]["REVIEW_CHECK_ALLOWLIST_JSON"]
        )
        if any(check.image != manifest["images"]["SANDBOX_IMAGE"] for check in checks.checks):
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise ReleaseError("rendered deployment does not match signed image identities") from exc
