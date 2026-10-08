"""Synthetic signed-document fixtures; injected verifier stubs are NOT live Sigstore evidence."""

import json

import pytest

from infra.release.manifest import (
    CONFIG_FILES,
    IMAGE_KEYS,
    OWN_IMAGES,
    create_manifest,
    digest,
    write_json,
)


@pytest.fixture
def artifacts(tmp_path):
    repository = tmp_path / "repository"
    (repository / "migrations").mkdir(parents=True)
    (repository / "migrations/0001_initial.sql").write_text("SELECT 1;\n")
    for name in CONFIG_FILES:
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public reviewed configuration\n")

    def make(name, commit, previous=None):
        directory = tmp_path / name
        directory.mkdir()
        images = {
            key: f"ghcr.io/test/{key.lower()}@sha256:{i + 1:064x}"
            for i, key in enumerate(IMAGE_KEYS)
        }
        for key in OWN_IMAGES:
            write_json(
                directory / f"{key}.scan.json",
                {
                    "SchemaVersion": 2,
                    "ArtifactType": "container_image",
                    "ArtifactName": images[key],
                    "Results": [{"Target": "fixture", "Vulnerabilities": []}],
                },
            )
            write_json(directory / f"{key}.sbom.json", {"bomFormat": "CycloneDX", "components": []})
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
