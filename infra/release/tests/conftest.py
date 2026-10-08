"""Synthetic signed-document fixtures; injected verifier stubs are NOT live Sigstore evidence."""

import json
import shutil
from pathlib import Path

import pytest

from infra.release.manifest import (
    CONFIG_FILES,
    IMAGE_KEYS,
    create_manifest,
    digest,
    write_json,
)


@pytest.fixture
def artifacts(tmp_path):
    repository = tmp_path / "repository"
    source = Path(__file__).parents[3]
    shutil.copytree(source / "migrations", repository / "migrations")
    for name in CONFIG_FILES:
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, path)

    def make(name, commit, previous=None):
        directory = tmp_path / name
        directory.mkdir()
        images = {
            key: f"ghcr.io/test/{key.lower()}@sha256:{i + 1:064x}"
            for i, key in enumerate(IMAGE_KEYS)
        }
        for key in IMAGE_KEYS:
            write_json(
                directory / f"{key}.scan.json",
                {
                    "SchemaVersion": 2,
                    "ArtifactType": "container_image",
                    "ArtifactName": images[key],
                    "Results": [{"Target": "fixture", "Class": "os-pkgs", "Vulnerabilities": []}],
                },
            )
            write_json(
                directory / f"{key}.sbom.json",
                {
                    "bomFormat": "CycloneDX",
                    "metadata": {"component": {"type": "container", "name": images[key]}},
                    "components": [{"type": "library", "name": "fixture", "version": "1"}],
                },
            )
        prior = None if previous is None else json.loads((previous / "release.json").read_text())
        manifest = create_manifest(
            repository,
            commit,
            images,
            directory,
            prior,
            None if previous is None else digest(previous / "release.json"),
        )
        write_json(directory / "release.json", manifest)
        (directory / "release.sigstore.json").write_text("{}")
        return directory

    previous = make("previous", "1" * 40)
    candidate = make("candidate", "2" * 40, previous)
    return repository, candidate, previous
