"""Publication stays draft on absent/mismatched uploaded evidence; never overwrites tags."""

import shutil

import pytest

from infra.release import publish
from infra.release.manifest import ReleaseError, read_json


def test_publication_requires_separate_authorization(monkeypatch, tmp_path):
    def unexpected(*_, **__):
        pytest.fail("gate must not run without authorization")

    monkeypatch.setattr(publish, "release_gate", unexpected)
    with pytest.raises(ReleaseError, match="separate explicit"):
        publish.publish(tmp_path, tmp_path, tmp_path, "a" * 64)


@pytest.mark.parametrize("mismatch", ["attachment", "tag", "existing_tag", "none"])
def test_draft_and_uploaded_bytes_are_checked_before_publication(
    artifacts, tmp_path, monkeypatch, mismatch
):
    _, candidate, previous = artifacts
    manifest = read_json(candidate / "release.json")
    receipt = tmp_path / "staging.json"
    receipt.write_text("verified by gate fixture")
    receipt.with_suffix(".sigstore.json").write_text("{}")
    monkeypatch.setattr(publish, "release_gate", lambda *_: manifest)
    commands = []
    attached = []

    def output(command):
        if command[:2] == ["git", "ls-remote"]:
            return "existing" if mismatch == "existing_tag" else ""
        return "1" * 40 if mismatch == "tag" else manifest["commit"]

    def run(command):
        commands.append(command)
        if command[:3] == ["gh", "release", "create"]:
            attached.extend(command[command.index("--notes-file") + 2 :])
        if command[:3] == ["gh", "release", "download"]:
            destination = command[command.index("--dir") + 1]
            for path in attached:
                shutil.copy(path, destination)
            if mismatch == "attachment":
                from pathlib import Path

                (Path(destination) / "release.json").write_text("forged")

    monkeypatch.setattr(publish, "output", output)
    monkeypatch.setattr(publish, "run_checked", run)
    if mismatch != "none":
        with pytest.raises(ReleaseError):
            publish.publish(candidate, previous, receipt, "a" * 64, authorized=True)
        assert not any(command[:3] == ["gh", "release", "edit"] for command in commands)
    else:
        publish.publish(candidate, previous, receipt, "a" * 64, authorized=True)
        assert "--draft" in commands[0]
        assert commands[-1][-1] == "--draft=false"
    if mismatch == "existing_tag":
        assert commands == []
