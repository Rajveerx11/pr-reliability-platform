"""Real staging checks must execute in order; any failure prevents a receipt."""

from pathlib import Path

import pytest

from infra.release import staging
from infra.release.manifest import ReleaseError, digest, read_json


@pytest.fixture
def staging_tools(artifacts, tmp_path, monkeypatch):
    repository, candidate, previous = artifacts
    program = tmp_path / "real-provider-e2e"
    program.write_text("fixture only")
    monkeypatch.setattr(staging, "E2E_PROGRAM", program)
    monkeypatch.setattr(staging.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        staging, "verify_release", lambda directory, **_: read_json(directory / "release.json")
    )
    candidate_env, previous_env = tmp_path / "candidate.env", tmp_path / "previous.env"
    candidate_env.touch()
    previous_env.touch()
    events = []

    def environment(_, env):
        return {
            "RELEASE_DIRECTORY": str(candidate if env.name == candidate_env.name else previous),
            **dict.fromkeys(
                (
                    "PRIVATE_BASE_URL",
                    "PRIVATE_BIND_ADDRESS",
                    "DATABASE_URL",
                    "SANDBOX_DOCKER_SOCKET",
                    "SANDBOX_STAGING_DIRECTORY",
                ),
                "same-staging",
            ),
            "BACKUP_DIRECTORY": str(tmp_path / "backups"),
        }

    monkeypatch.setattr(staging, "validate_environment", environment)

    def execute(command, *_):
        events.append("e2e" if command[0] == str(program) else "deploy")

    monkeypatch.setattr(staging, "checked", execute)
    monkeypatch.setattr(staging, "validate_rendered_deployment", lambda *_: None)
    monkeypatch.setattr(staging, "check_health", lambda *_, **__: events.append("health"))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text("backup fixture")

    def backup(*_, **__):
        events.append("backup")
        return bundle

    monkeypatch.setattr(staging, "backup", backup)
    monkeypatch.setattr(staging, "restore", lambda *_, **__: events.append("restore"))
    return (
        repository,
        candidate,
        previous,
        candidate_env,
        previous_env,
        tmp_path / "staging.json",
        "123",
        digest(program),
    ), events


def test_staging_executes_backup_restore_rollback_and_repromotion(staging_tools):
    arguments, events = staging_tools
    receipt = staging.exercise_staging(*arguments, authorized=True)
    assert events == [
        "deploy",
        "health",
        "e2e",
        "backup",
        "restore",
        "health",
        "e2e",
        "deploy",
        "health",
        "e2e",
        "deploy",
        "health",
        "e2e",
    ]
    assert receipt["checks"] == dict.fromkeys(staging.CHECKS, "passed")
    assert Path(arguments[5]).is_file()


@pytest.mark.parametrize("operation", ["check_health", "backup", "restore", "checked"])
def test_any_failed_operation_produces_no_acceptance_receipt(staging_tools, monkeypatch, operation):
    arguments, _ = staging_tools

    def reject(*_, **__):
        raise ReleaseError("live check failed")

    monkeypatch.setattr(staging, operation, reject)
    with pytest.raises(ReleaseError, match="live check failed"):
        staging.exercise_staging(*arguments, authorized=True)
    assert not arguments[5].exists()


def test_staging_without_authorization_or_linux_has_no_side_effects(staging_tools, monkeypatch):
    arguments, events = staging_tools
    with pytest.raises(ReleaseError, match="authorization"):
        staging.exercise_staging(*arguments)
    monkeypatch.setattr(staging.platform, "system", lambda: "Windows")
    with pytest.raises(ReleaseError, match="Linux"):
        staging.exercise_staging(*arguments, authorized=True)
    assert not events


def test_e2e_checksum_and_previous_target_fail_before_deploy(staging_tools):
    arguments, events = staging_tools
    with pytest.raises(ReleaseError, match="checksum"):
        staging.exercise_staging(*arguments[:-1], "f" * 64, authorized=True)
    previous_path = arguments[2] / "release.json"
    previous_path.write_text(previous_path.read_text().replace('"111111', '"311111', 1))
    with pytest.raises(ReleaseError):
        staging.exercise_staging(*arguments, authorized=True)
    assert not events


@pytest.mark.parametrize("target", ["candidate", "previous", "environment", "same_commit_build"])
def test_mutable_release_inputs_never_produce_receipt(staging_tools, monkeypatch, target):
    import json

    arguments, _ = staging_tools
    original = staging.checked
    count = 0

    def mutate(command, values):
        nonlocal count
        original(command, values)
        if command[0] == str(staging.E2E_PROGRAM):
            count += 1
            if count == 4:
                if target == "environment":
                    arguments[3].write_text("PLATFORM_IMAGE=unsigned")
                else:
                    path = arguments[2 if target == "previous" else 1] / "release.json"
                    if target == "same_commit_build":
                        replacement = read_json(path)
                        replacement["images"]["PLATFORM_IMAGE"] = (
                            "ghcr.io/another/build@sha256:" + "f" * 64
                        )
                        path.write_text(json.dumps(replacement))
                    else:
                        path.write_text("changed")

    monkeypatch.setattr(staging, "checked", mutate)
    with pytest.raises(ReleaseError, match="changed during"):
        staging.exercise_staging(*arguments, authorized=True)
    assert not arguments[5].exists()
