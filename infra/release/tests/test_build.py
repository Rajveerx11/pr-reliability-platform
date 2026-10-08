"""Build controls reject dirty source and vulnerabilities before registry publication."""

import json
import shutil
from pathlib import Path

import pytest

from infra.release import build
from infra.release.manifest import IMAGE_KEYS, OWN_IMAGES, ReleaseError, read_json


def test_dirty_or_wrong_commit_is_not_built(artifacts, tmp_path, monkeypatch):
    repository, _, _ = artifacts
    commands = []

    def output(command):
        commands.append(command)
        return "2" * 40 if command[3] == "rev-parse" else "?? unrelated.env"

    monkeypatch.setattr(build, "output", output)
    with pytest.raises(ReleaseError, match="clean"):
        build.build(repository, "2" * 40, tmp_path / "upstream.json", tmp_path / "out")
    assert all(command[0] == "git" for command in commands)


@pytest.mark.parametrize("scanner", ["source_secret", "secret", "vuln", "functionality"])
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
        if command[0] == "git" and command[3:] == ["rev-parse", "HEAD"]:
            return "2" * 40
        if (scanner == "source_secret" and command[:2] == ["trivy", "fs"]) or (
            scanner == "functionality" and command[:2] == ["docker", "run"]
        ):
            raise ReleaseError("scanner rejected image")
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
    archive = next(
        command for command in commands if command[0] == "git" and command[3] == "archive"
    )
    assert archive[-1] == "2" * 40
    assert archive[1:3] == ["-C", str(repository.resolve())]
    if scanner != "source_secret":
        docker = next(command for command in commands if command[:2] == ["docker", "build"])
        assert Path(docker[-1]).name == "context"
        assert repository != Path(docker[-1])
        assert "--provenance=false" in docker and "--sbom=false" in docker


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
        if command[0] == "git" and command[3:] == ["rev-parse", "HEAD"]:
            return "2" * 40
        if command[0] == "tar":
            shutil.copytree(repository, command[-1], dirs_exist_ok=True)
        if command[:3] == ["docker", "image", "inspect"]:
            suffix = command[-1].split("/")[-1].split(":")[0]
            key = dict(zip(("platform", "provider-activity", "sandbox"), OWN_IMAGES, strict=True))[
                suffix
            ]
            return fixture_manifest["images"][key]
        if command[:2] == ["trivy", "image"] and "--output" in command:
            path = Path(command[command.index("--output") + 1])
            shutil.copyfile(candidate / path.name, path)
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
    smoke = [c for c in commands if c[:2] == ["docker", "run"]]
    assert len(smoke) == 3
    for command in smoke:
        assert "--network=none" in command and "--read-only" in command
        assert "--cap-drop=ALL" in command and "--security-opt=no-new-privileges" in command
        assert not any(arg in command for arg in ("--volume", "--env", "--privileged"))
    assert "git --version" in smoke[1][-1]
    assert "python -m pytest --version" in smoke[2][-1]
    assert result["rollback"]["commit"] == "1" * 40
    provider = [c for c in commands if c[:2] == ["docker", "build"]][1]
    assert (
        provider[provider.index("--build-arg") + 1]
        == "PLATFORM_IMAGE=" + result["images"]["PLATFORM_IMAGE"]
    )
