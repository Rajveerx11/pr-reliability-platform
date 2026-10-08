"""Build controls reject dirty source and vulnerabilities before registry publication."""

import json
from pathlib import Path

import pytest

from infra.release import build
from infra.release.manifest import IMAGE_KEYS, OWN_IMAGES, ReleaseError, read_json


def test_dirty_or_wrong_commit_is_not_built(artifacts, tmp_path, monkeypatch):
    repository, _, _ = artifacts
    commands = []

    def output(command):
        commands.append(command)
        return "2" * 40 if command[1] == "rev-parse" else "?? unrelated.env"

    monkeypatch.setattr(build, "output", output)
    with pytest.raises(ReleaseError, match="clean"):
        build.build(repository, "2" * 40, tmp_path / "upstream.json", tmp_path / "out")
    assert all(command[0] == "git" for command in commands)


@pytest.mark.parametrize("scanner", ["secret", "vuln"])
def test_scanning_fails_before_push_or_sign(artifacts, tmp_path, monkeypatch, scanner):
    repository, candidate, _ = artifacts
    images = read_json(candidate / "release.json")["images"]
    upstream = tmp_path / "upstream.json"
    upstream.write_text(
        json.dumps({key: images[key] for key in IMAGE_KEYS if key not in OWN_IMAGES})
    )
    commands = []

    def output(command):
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "2" * 40
        if (
            command[:2] == ["trivy", "image"]
            and command[command.index("--scanners") + 1] == scanner
        ):
            raise ReleaseError("scanner rejected image")
        return ""

    monkeypatch.setattr(build, "output", output)
    monkeypatch.setattr(build, "run_checked", commands.append)
    with pytest.raises(ReleaseError, match="scanner rejected"):
        build.build(repository, "2" * 40, upstream, tmp_path / "out")
    assert ["docker", "push"] not in [command[:2] for command in commands]
    assert not any(command[0] == "cosign" for command in commands)
    archive = next(command for command in commands if command[:2] == ["git", "archive"])
    assert archive[-1] == "2" * 40
    docker = next(command for command in commands if command[:2] == ["docker", "build"])
    assert Path(docker[-1]).name == "context"
    assert repository != Path(docker[-1])


def test_all_three_images_are_scanned_pushed_attested_and_manifest_bound(
    artifacts, tmp_path, monkeypatch
):
    repository, candidate, previous = artifacts
    fixture_manifest = read_json(candidate / "release.json")
    upstream = tmp_path / "upstream.json"
    upstream.write_text(
        json.dumps(
            {key: fixture_manifest["images"][key] for key in IMAGE_KEYS if key not in OWN_IMAGES}
        )
    )
    commands = []

    def output(command):
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "2" * 40
        if command[:3] == ["docker", "image", "inspect"]:
            suffix = command[-1].split("/")[-1].split(":")[0]
            key = dict(zip(("platform", "provider-activity", "sandbox"), OWN_IMAGES, strict=True))[
                suffix
            ]
            return fixture_manifest["images"][key]
        if command[:2] == ["trivy", "image"] and "--output" in command:
            path = Path(command[command.index("--output") + 1])
            if command[command.index("--format") + 1] == "json":
                path.write_text(
                    json.dumps(
                        {
                            "SchemaVersion": 2,
                            "ArtifactType": "container_image",
                            "ArtifactName": command[-1],
                            "Results": [{"Vulnerabilities": []}],
                        }
                    )
                )
            else:
                path.write_text(json.dumps({"bomFormat": "CycloneDX", "components": []}))
        return ""

    def sign(command):
        commands.append(command)
        if command[1] == "sign-blob":
            Path(command[command.index("--bundle") + 1]).write_text("{}")

    monkeypatch.setattr(build, "output", output)
    monkeypatch.setattr(build, "run_checked", sign)
    monkeypatch.setattr(
        build, "verify_release", lambda directory, **_: read_json(directory / "release.json")
    )
    result = build.build(repository, "2" * 40, upstream, tmp_path / "out", previous)
    assert set(result["images"]) == set(IMAGE_KEYS)
    assert len(result["evidence"]) == 6
    assert len([c for c in commands if c[:2] == ["docker", "push"]]) == 3
    assert len([c for c in commands if c[:2] == ["cosign", "attest"]]) == 3
    assert result["rollback"]["commit"] == "1" * 40
    provider = [c for c in commands if c[:2] == ["docker", "build"]][1]
    assert (
        provider[provider.index("--build-arg") + 1]
        == "PLATFORM_IMAGE=" + result["images"]["PLATFORM_IMAGE"]
    )
