"""Check container state and private TLS readiness for one deployed VM."""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

from .preflight import load_environment

_RUNNING_SERVICES = {
    "api",
    "command-dispatcher",
    "workflow-worker",
    "activity-worker",
    "postgres",
    "temporal",
    "otel-collector",
    "prometheus",
    "caddy",
}


class HealthCheckError(RuntimeError):
    """One required deployment health check failed."""


def check_health(
    compose_file: Path,
    environment_file: Path,
    *,
    command_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    url_reader: Callable[[str, ssl.SSLContext | None], tuple[int, bytes]] | None = None,
) -> None:
    """Require every long-running service, API readiness, TLS, and Prometheus readiness."""
    values = load_environment(environment_file)
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(environment_file.resolve(strict=True)),
        "--file",
        str(compose_file.resolve(strict=True)),
    ]
    run = command_runner or _run
    result = run([*compose, "ps", "--status", "running", "--services"])
    if result.returncode != 0:
        raise HealthCheckError("could not inspect deployment services")
    running = set(result.stdout.splitlines())
    missing = sorted(_RUNNING_SERVICES - running)
    if missing:
        raise HealthCheckError(f"deployment services are not running: {', '.join(missing)}")

    try:
        tls_context = ssl.create_default_context(cafile=values["TLS_CA_FILE"])
    except (KeyError, OSError) as exc:
        raise HealthCheckError("TLS trust file is unavailable") from exc
    read = url_reader or _read_url
    base_url = values.get("PRIVATE_BASE_URL", "").rstrip("/")
    status, body = read(f"{base_url}/health/ready", tls_context)
    if status != 200:
        raise HealthCheckError("private API readiness failed")
    try:
        readiness = json.loads(body)
    except ValueError as exc:
        raise HealthCheckError("private API readiness returned invalid JSON") from exc
    if readiness.get("status") != "ready":
        raise HealthCheckError("private API dependencies are not ready")

    prometheus_url = values.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
    status, _ = read(prometheus_url + "/-/ready", None)
    if status != 200:
        raise HealthCheckError("Prometheus readiness failed")
    status, body = read(
        prometheus_url + "/api/v1/query?query=up%7Bjob%3D%22otel-collector%22%7D",
        None,
    )
    try:
        metrics = json.loads(body)
        samples = metrics["data"]["result"]
        collector_up = status == 200 and len(samples) == 1 and samples[0]["value"][1] == "1"
    except (KeyError, TypeError, ValueError):
        collector_up = False
    if not collector_up:
        raise HealthCheckError("OpenTelemetry metrics scrape failed")


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _read_url(url: str, context: ssl.SSLContext | None) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5, context=context) as response:
        return response.status, response.read(64 * 1024)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compose-file", type=Path, default=Path(__file__).with_name("compose.vm.yaml")
    )
    parser.add_argument("--env-file", type=Path, required=True)
    arguments = parser.parse_args()
    check_health(arguments.compose_file, arguments.env_file)
    print("deployment health passed")


if __name__ == "__main__":
    main()
