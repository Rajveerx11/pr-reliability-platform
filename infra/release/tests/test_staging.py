"""Real staging checks must execute in order; any failure prevents a receipt."""

import json
from pathlib import Path

import pytest

from infra.deployment.preflight import load_environment
from infra.release import staging, verify
from infra.release.manifest import IMAGE_KEYS, ReleaseError, digest, read_json


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
    candidate_env.write_text(f"RELEASE_DIRECTORY={candidate}\n")
    previous_env.write_text(f"RELEASE_DIRECTORY={previous}\n")
    events = []

    def environment(_, env):
        return {
            **load_environment(env),
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


def test_staging_rejects_environment_selecting_another_release(staging_tools):
    arguments, events = staging_tools
    arguments[3].write_text(f"RELEASE_DIRECTORY={arguments[2]}\n")
    with pytest.raises(ReleaseError, match="selected signed releases"):
        staging.exercise_staging(*arguments, authorized=True)
    assert not events
    assert not arguments[5].exists()


@pytest.mark.parametrize("configure_b", [False, True])
def test_transient_same_commit_swap_cannot_deploy_b_and_attest_a(
    staging_tools, monkeypatch, configure_b
):
    arguments, events = staging_tools
    _, candidate, previous, candidate_env, previous_env, output, *_ = arguments
    manifest_a = read_json(candidate / "release.json")
    sha_a = digest(candidate / "release.json")
    original = {path.name: path.read_bytes() for path in candidate.iterdir()}
    manifest_b = json.loads(json.dumps(manifest_a))
    image_b = "ghcr.io/test/another-build@sha256:" + "f" * 64
    manifest_b["images"]["PLATFORM_IMAGE"] = image_b
    scan_b = read_json(candidate / "PLATFORM_IMAGE.scan.json")
    scan_b["ArtifactName"] = image_b
    sbom_b = read_json(candidate / "PLATFORM_IMAGE.sbom.json")
    sbom_b["metadata"]["component"]["name"] = image_b

    for directory, env in ((candidate, candidate_env), (previous, previous_env)):
        manifest = read_json(directory / "release.json")
        values = {
            "RELEASE_DIRECTORY": str(directory),
            "RELEASE_COMMIT": manifest["commit"],
            "DEPLOYMENT_CONFIG_VERSION": manifest["config_version"],
            **{key: manifest["images"][key] for key in IMAGE_KEYS if key != "SANDBOX_IMAGE"},
            "REVIEW_CHECK_ALLOWLIST_JSON": json.dumps(
                [
                    {
                        "name": "pytest",
                        "command": ["pytest"],
                        "image": manifest["images"]["SANDBOX_IMAGE"],
                    }
                ]
            ),
        }
        if configure_b and directory == candidate:
            values["PLATFORM_IMAGE"] = image_b
        env.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")

    verified = []
    preflight = []
    deployed = []

    def authenticate(directory, **kwargs):
        # Swap the mutable source A→B only while authentication/preflight run, then
        # restore A before unchanged() checks. B has valid same-commit evidence;
        # only Cosign is synthetic, not manifest/evidence/preflight verification.
        (candidate / "PLATFORM_IMAGE.scan.json").write_text(json.dumps(scan_b))
        (candidate / "PLATFORM_IMAGE.sbom.json").write_text(json.dumps(sbom_b))
        for name in ("PLATFORM_IMAGE.scan.json", "PLATFORM_IMAGE.sbom.json"):
            manifest_b["evidence"][name] = digest(candidate / name)
        (candidate / "release.json").write_text(json.dumps(manifest_b))
        result = verify.verify_release(directory, runner=lambda _: None, **kwargs)
        verified.append((directory, result))
        return result

    original_environment = staging.validate_environment

    def environment(repo, env):
        values = original_environment(repo, env)
        try:
            result = verify.validate_deployment_release(repo, values, runner=lambda _: None)
            preflight.append((Path(values["RELEASE_DIRECTORY"]), result))
        finally:
            if configure_b or env.name == "previous.env":
                for name, content in original.items():
                    (candidate / name).write_bytes(content)
        return values

    original_checked = staging.checked

    def checked(command, values):
        original_checked(command, values)
        if command[0] == "docker":
            # These are the exact process values supplied to Compose, not source claims.
            deployed.append(values["PLATFORM_IMAGE"])
            assert Path(values["RELEASE_DIRECTORY"]) not in (candidate, previous)

    monkeypatch.setattr(staging, "verify_release", authenticate)
    monkeypatch.setattr(staging, "validate_environment", environment)
    monkeypatch.setattr(verify, "validate_rendered_deployment", lambda *_: None)
    monkeypatch.setattr(staging, "checked", checked)
    if configure_b:
        with pytest.raises(ReleaseError, match="deployment image does not match"):
            staging.exercise_staging(*arguments, authorized=True)
        assert not events
        assert not output.exists()
        assert digest(candidate / "release.json") == sha_a
        return
    receipt = staging.exercise_staging(*arguments, authorized=True)
    assert [manifest for _, manifest in verified] == [
        manifest_a,
        read_json(previous / "release.json"),
    ]
    assert [manifest for _, manifest in preflight] == [manifest for _, manifest in verified]
    assert [directory for directory, _ in preflight] == [directory for directory, _ in verified]
    assert all(directory not in (candidate, previous) for directory, _ in verified)
    assert deployed == [manifest_a["images"]["PLATFORM_IMAGE"]] * 3
    assert image_b not in deployed
    assert digest(candidate / "release.json") == sha_a == receipt["manifest_sha256"]
    assert receipt["checks"] == dict.fromkeys(staging.CHECKS, "passed")
    assert output.is_file()
    assert "restore" in events
