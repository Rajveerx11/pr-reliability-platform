"""Supported Compose configuration really installs monitor dependencies and probes."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize(
    "manifest", ["infra/compose/compose.yaml", "infra/deployment/compose.vm.yaml"]
)
def test_rendered_compose_worker_readiness_grace_credentials_and_periodic_probes(manifest):
    if not shutil.which("docker"):
        if os.environ.get("CI"):
            pytest.fail("docker compose required for deployed config regression")
        pytest.skip("docker compose unavailable")
    environment = os.environ | {"DASHBOARD_BASE_URL": "https://reviews.internal.example/dashboard"}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / "infra/deployment/deployment.env.example"),
            "-f",
            str(ROOT / manifest),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    services = json.loads(result.stdout)["services"]
    for name in ("workflow-worker", "activity-worker"):
        worker = services[name]
        env = worker["environment"]
        assert {"DATABASE_URL", "OWNER_ID", "RUNNER_ID", "RUNNER_VERSION"} <= env.keys()
        assert all(env[key] for key in ("DATABASE_URL", "OWNER_ID", "RUNNER_ID", "RUNNER_VERSION"))
        assert worker["stop_grace_period"] == "1m30s"
        assert worker["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
        if "vm.yaml" in manifest:
            assert worker["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert (
        services["api"]["environment"]["TEMPORAL_TASK_QUEUE"]
        == services["workflow-worker"]["environment"]["TEMPORAL_TASK_QUEUE"]
    )
    workflow = services["workflow-worker"]
    assert not any(
        key.startswith(("GITHUB_", "OPENAI_", "MODEL_")) for key in workflow["environment"]
    )
    assert not workflow.get("secrets") and not workflow.get("volumes")
    monitor = services["operations-monitor"]
    assert monitor["command"] == [
        "pr-reliability-operation-alerts",
        "--config",
        "/run/operations/config.json",
    ]
    assert "OPERATIONS_ALERT_TOKEN_FILE" in monitor["environment"]
    assert "OPERATIONS_ALERT_TOKEN" not in monitor["environment"]
    targets = {volume["target"]: volume for volume in monitor["volumes"]}
    assert {
        "/probes/disk",
        "/probes/backup",
        "/probes/tls.pem",
        "/run/operations/config.json",
        "/run/operations/token",
    } <= targets.keys()
    assert all(volume["read_only"] for volume in targets.values())
    assert "operations-monitor:9108" in (ROOT / "infra/deployment/prometheus.yaml").read_text()
    rules = (ROOT / "infra/deployment/rules.yaml").read_text()
    assert (
        "OperationsMonitorUnavailable" in rules
        and "pr_operations_alert_last_success_seconds > 120" in rules
    )
    assert "prometheus" in services


def test_installed_backup_job_produces_receipt_at_the_probe_mount():
    service = (ROOT / "infra/deployment/pr-reliability-backup.service").read_text()
    assert "--receipt /var/lib/pr-reliability/operations/backup.json" in service
    assert "StateDirectory=pr-reliability/operations" in service
    config = json.loads((ROOT / "infra/observability/operations-alerts.example.json").read_text())
    assert config["backup_receipt"] == "/probes/backup/backup.json"
    assert config["disk_path"] == "/probes/disk"
    assert config["tls_certificate"] == "/probes/tls.pem"
