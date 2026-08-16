"""Deployment health aggregation tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from infra.deployment import health
from infra.deployment.health import HealthCheckError, check_health

SERVICES = """api
command-dispatcher
workflow-worker
activity-worker
postgres
temporal
otel-collector
prometheus
caddy"""


def _files(tmp_path: Path) -> tuple[Path, Path]:
    compose = tmp_path / "compose.yaml"
    compose.touch()
    environment = tmp_path / "deployment.env"
    environment.write_text(
        "PRIVATE_BASE_URL=https://reviews.internal.example\n"
        f"TLS_CA_FILE={tmp_path / 'ca.crt'}\n"
        "PROMETHEUS_URL=http://127.0.0.1:9090\n",
        encoding="utf-8",
    )
    return compose, environment


def test_health_requires_running_services_tls_api_and_prometheus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose, environment = _files(tmp_path)
    tls_context = object()
    monkeypatch.setattr(health.ssl, "create_default_context", lambda **_: tls_context)
    requested: list[tuple[str, object | None]] = []

    def read(url: str, context: object | None) -> tuple[int, bytes]:
        requested.append((url, context))
        if url.endswith("/health/ready"):
            return 200, b'{"status":"ready"}'
        if "/api/v1/query" in url:
            return 200, b'{"status":"success","data":{"result":[{"value":[1,"1"]}]}}'
        return 200, b"Prometheus is Ready."

    check_health(
        compose,
        environment,
        command_runner=lambda _: subprocess.CompletedProcess([], 0, SERVICES, ""),
        url_reader=read,
    )

    assert requested == [
        ("https://reviews.internal.example/health/ready", tls_context),
        ("http://127.0.0.1:9090/-/ready", None),
        (
            "http://127.0.0.1:9090/api/v1/query?query=up%7Bjob%3D%22otel-collector%22%7D",
            None,
        ),
    ]


def test_health_fails_when_one_service_is_missing(tmp_path: Path) -> None:
    compose, environment = _files(tmp_path)

    with pytest.raises(HealthCheckError, match="caddy"):
        check_health(
            compose,
            environment,
            command_runner=lambda _: subprocess.CompletedProcess(
                [], 0, SERVICES.replace("caddy", ""), ""
            ),
        )


def test_health_fails_when_collector_is_not_scraped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose, environment = _files(tmp_path)
    monkeypatch.setattr(health.ssl, "create_default_context", lambda **_: object())

    def read(url: str, _context: object | None) -> tuple[int, bytes]:
        if url.endswith("/health/ready"):
            return 200, b'{"status":"ready"}'
        if "/api/v1/query" in url:
            return 200, b'{"status":"success","data":{"result":[]}}'
        return 200, b"ready"

    with pytest.raises(HealthCheckError, match="metrics scrape"):
        check_health(
            compose,
            environment,
            command_runner=lambda _: subprocess.CompletedProcess([], 0, SERVICES, ""),
            url_reader=read,
        )
