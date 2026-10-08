"""Manual-only release entrypoints, pinned actions and least-privilege separation."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]


def test_release_workflows_are_manual_and_actions_are_commit_pinned():
    for name in ("release-build.yml", "release-staging.yml", "release-publication.yml"):
        document = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        events = document.get("on", document.get(True))  # PyYAML uses YAML 1.1 booleans.
        assert set(events) == {"workflow_dispatch"}
        assert document["permissions"] == {"contents": "read"}
        assert document["concurrency"]["cancel-in-progress"] is False
        for job in document["jobs"].values():
            assert "refs/heads/main" in job["if"]
            assert job["environment"]
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+@[0-9a-f]{40}", step["uses"])


def test_deployment_and_publication_require_distinct_explicit_approvals():
    def workflow(name):
        return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())

    staging = workflow("release-staging.yml")
    publication = workflow("release-publication.yml")
    for doc, authorization in (
        (staging, "authorize_disposable_staging"),
        (publication, "authorize_github_release_publication"),
    ):
        events = doc.get("on", doc.get(True))
        assert events["workflow_dispatch"]["inputs"][authorization]["default"] is False
    assert staging["jobs"]["staging"]["permissions"]["contents"] == "read"
    assert publication["jobs"]["publish"]["permissions"]["contents"] == "write"
    assert "id-token" not in publication["jobs"]["publish"]["permissions"]
    assert staging["jobs"]["staging"]["runs-on"] == [
        "self-hosted",
        "linux",
        "x64",
        "pr-release-staging",
    ]


def test_release_sandbox_has_locked_tools_without_source_or_credentials():
    sandbox = (ROOT / "infra/release/Dockerfile.sandbox").read_text()
    assert "--frozen" in sandbox and "--require-hashes" in sandbox
    assert "USER 65534:65534" in sandbox
    assert "COPY apps" not in sandbox and "COPY workers" not in sandbox
    assert "ARG " not in sandbox
    provider = (ROOT / "infra/release/Dockerfile.activity").read_text()
    assert "ARG PLATFORM_IMAGE" in provider
    assert "USER app" in provider
    assert "snapshot.debian.org" in provider
    assert "docker:28.5.1-cli@sha256:" in provider


def test_all_release_clis_use_frozen_project_dependencies():
    for name in ("release-build.yml", "release-staging.yml", "release-publication.yml"):
        document = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        for job in document["jobs"].values():
            steps = job["steps"]
            install = next(
                i for i, step in enumerate(steps) if step.get("run") == "uv sync --frozen"
            )
            assert any(
                step.get("uses", "").startswith("astral-sh/setup-uv@") for step in steps[:install]
            )
            for index, step in enumerate(steps):
                if "-m infra.release" in step.get("run", ""):
                    assert index > install
                    assert "uv run python -m infra.release" in step["run"]


def test_uv_binary_is_exactly_pinned_in_all_release_workflows():
    for name in ("release-build", "release-staging", "release-publication"):
        content = (ROOT / f".github/workflows/{name}.yml").read_text()
        assert "          version: '0.12.7'" in content
