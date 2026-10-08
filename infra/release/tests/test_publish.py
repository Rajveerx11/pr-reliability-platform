"""Realistic Git ref/draft behavior, immutable attachments, retries and races (offline)."""

import json
from pathlib import Path

import pytest

from infra.release import publish
from infra.release.manifest import ReleaseError, read_json


def test_publication_requires_separate_authorization(monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "release_gate", lambda *_: pytest.fail("unexpected gate"))
    with pytest.raises(ReleaseError, match="separate explicit"):
        publish.publish(tmp_path, tmp_path, tmp_path, "a" * 64)


@pytest.fixture
def publication(artifacts, tmp_path, monkeypatch):
    _, candidate, previous = artifacts
    manifest = read_json(candidate / "release.json")
    receipt = tmp_path / "staging.json"
    receipt.write_text("verified by gate fixture")
    receipt.with_suffix(".sigstore.json").write_text("{}")
    monkeypatch.setattr(publish, "release_gate", lambda *_: manifest)
    state = {
        "tag": None,
        "draft": None,
        "assets": {},
        "commands": [],
        "race": False,
        "gate_calls": 0,
    }

    def output(command):
        state["commands"].append(command)
        if "git/matching-refs" in command[2]:
            return "ref" if state["tag"] else ""
        if "git/ref/tags" in command[2]:
            return json.dumps({"object": {"type": "commit", "sha": state["tag"]}})
        if "releases?" in command[2]:
            return (
                "" if state["draft"] is None else json.dumps({"id": 123, "draft": state["draft"]})
            )
        pytest.fail(f"unexpected command {command}")

    def run(command):
        state["commands"].append(command)
        if command[:3] == ["gh", "api", "--method"]:
            if state["race"]:
                state["tag"] = "1" * 40
                raise ReleaseError("GitHub ref already exists")
            assert state["tag"] is None
            state["tag"] = manifest["commit"]
        if command[:3] == ["gh", "release", "create"]:
            assert state["tag"] == manifest["commit"]
            assert "--verify-tag" in command and "--draft" in command
            assert state["draft"] is None
            state["draft"] = True  # A draft does NOT create a Git tag.
            state["assets"] = {
                Path(path).name: Path(path).read_bytes()
                for path in command[command.index("--notes-file") + 2 :]
            }
        if command[:3] == ["gh", "release", "download"]:
            for name, content in state["assets"].items():
                (Path(command[command.index("--dir") + 1]) / name).write_bytes(content)
        if command[:3] == ["gh", "release", "edit"]:
            assert state["draft"] is True
            state["draft"] = False

    monkeypatch.setattr(publish, "output", output)
    monkeypatch.setattr(publish, "run_checked", run)
    return (candidate, previous, receipt, "a" * 64), state, manifest


def test_tag_is_created_and_verified_before_draft(publication):
    arguments, state, _ = publication
    publish.publish(*arguments, authorized=True)
    commands = state["commands"]
    create = next(i for i, c in enumerate(commands) if c[:3] == ["gh", "release", "create"])
    assert any(c[:3] == ["gh", "api", "--method"] for c in commands[:create])
    assert any("git/ref/tags" in c[2] for c in commands[:create])
    assert state["draft"] is False


@pytest.mark.parametrize("failure", ["wrong_tag", "published", "race"])
def test_conflicts_and_tag_creation_races_do_not_publish(publication, failure):
    arguments, state, manifest = publication
    state["tag"] = "1" * 40 if failure == "wrong_tag" else None
    if failure == "published":
        state.update(tag=manifest["commit"], draft=False)
    state["race"] = failure == "race"
    with pytest.raises(ReleaseError):
        publish.publish(*arguments, authorized=True)
    assert not any(c[:3] == ["gh", "release", "edit"] for c in state["commands"])


def test_tag_only_retry_and_complete_draft_retry(publication):
    arguments, state, manifest = publication
    state["tag"] = manifest["commit"]  # Earlier attempt stopped after ref creation.
    publish.publish(*arguments, authorized=True)
    state.update(draft=True, commands=[])
    publish.publish(*arguments, authorized=True)
    assert not any(c[:3] == ["gh", "release", "create"] for c in state["commands"])
    assert state["draft"] is False


@pytest.mark.parametrize("mutation", ["attachment", "original", "same_commit_build", "tag_race"])
def test_final_mutations_cannot_publish(publication, monkeypatch, mutation):
    arguments, state, manifest = publication
    calls = 0

    def gate(candidate, *_):
        nonlocal calls
        calls += 1
        if calls == 2:
            if mutation == "original":
                (arguments[0] / "release.json").write_text("swapped")
            elif mutation == "same_commit_build":
                replacement = {
                    **manifest,
                    "images": {
                        **manifest["images"],
                        "PLATFORM_IMAGE": "ghcr.io/other/build@sha256:" + "f" * 64,
                    },
                }
                (arguments[0] / "release.json").write_text(json.dumps(replacement))
                return replacement
            elif mutation == "tag_race":
                state["tag"] = "1" * 40
        return manifest

    monkeypatch.setattr(publish, "release_gate", gate)
    if mutation == "attachment":
        original = publish.run_checked

        def altered(command):
            original(command)
            if command[:3] == ["gh", "release", "create"]:
                state["assets"]["release.json"] = b"forged"

        monkeypatch.setattr(publish, "run_checked", altered)
    with pytest.raises(ReleaseError):
        publish.publish(*arguments, authorized=True)
    assert not any(c[:3] == ["gh", "release", "edit"] for c in state["commands"])
