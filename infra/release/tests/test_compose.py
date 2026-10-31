"""Ambient image/Compose overrides must never change authenticated deployment identities."""

import json
import subprocess

import pytest

from infra.release import compose
from infra.release.manifest import ReleaseError, read_json
from infra.release.tests.test_verify import deployment_values


@pytest.mark.parametrize("mutation", ["none", "image", "sandbox", "service", "invalid", "failure"])
def test_controlled_rendering_checks_every_image(artifacts, monkeypatch, mutation):
    repository, candidate, _ = artifacts
    manifest = read_json(candidate / "release.json")
    values = deployment_values(candidate)
    monkeypatch.setenv("PLATFORM_IMAGE", "ghcr.io/host/unsigned@sha256:" + "f" * 64)
    monkeypatch.setenv("COMPOSE_FILE", "/host/override.yaml")
    monkeypatch.setenv("DOCKER_HOST", "tcp://host:2375")
    services = {
        name: {"image": manifest["images"][key]} for name, key in compose.SERVICE_IMAGES.items()
    }
    services["activity-worker"]["environment"] = {
        "REVIEW_CHECK_ALLOWLIST_JSON": values["REVIEW_CHECK_ALLOWLIST_JSON"]
    }
    if mutation == "image":
        services["api"]["image"] = "unsigned"
    if mutation == "sandbox":
        services["activity-worker"]["environment"]["REVIEW_CHECK_ALLOWLIST_JSON"] = values[
            "REVIEW_CHECK_ALLOWLIST_JSON"
        ].replace(manifest["images"]["SANDBOX_IMAGE"], "ghcr.io/other/check@sha256:" + "f" * 64)
    if mutation == "service":
        services["injected"] = {"image": "unsigned"}

    def run(command, **kwargs):
        assert kwargs["env"]["PLATFORM_IMAGE"] == values["PLATFORM_IMAGE"]
        assert "COMPOSE_FILE" not in kwargs["env"] and "DOCKER_HOST" not in kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            int(mutation == "failure"),
            b"invalid" if mutation == "invalid" else json.dumps({"services": services}).encode(),
            b"secret",
        )

    monkeypatch.setattr(compose.subprocess, "run", run)
    if mutation == "none":
        compose.validate_rendered_deployment(repository, values, manifest)
    else:
        with pytest.raises(ReleaseError):
            compose.validate_rendered_deployment(repository, values, manifest)


def test_all_staging_subprocess_adapters_use_controlled_environment(monkeypatch):
    monkeypatch.setenv("PLATFORM_IMAGE", "unsigned")
    monkeypatch.setenv("COMPOSE_PROFILES", "host")
    seen = []

    def run(command, **kwargs):
        seen.append(kwargs)
        assert kwargs["env"]["PLATFORM_IMAGE"] == "approved"
        assert "COMPOSE_PROFILES" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(compose.subprocess, "run", run)
    values = {"PLATFORM_IMAGE": "approved"}
    compose.checked(["docker", "compose", "up"], values)
    assert compose.database_runner(values)(["docker", "compose", "exec"], None, None) == 0
    compose.execute(["docker", "compose", "ps"], values)
    assert len(seen) == 3
