"""Retain exact artifact bytes across long-running release operations."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .manifest import ReleaseError, read_bytes, read_json, validate_manifest


@dataclass(frozen=True)
class Snapshot:
    directory: Path
    source: Path
    hashes: dict[str, str]

    def unchanged(self) -> None:
        for name, expected in self.hashes.items():
            for root in (self.source, self.directory):
                if hashlib.sha256(read_bytes(root / name)).hexdigest() != expected:
                    raise ReleaseError("release artifacts changed during operation")


@contextmanager
def snapshot_files(source: Path, names: tuple[str, ...]) -> Iterator[Snapshot]:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        hashes = {}
        for name in names:
            content = read_bytes(source / name)
            target = directory / name
            target.write_bytes(content)
            target.chmod(0o400)
            hashes[name] = hashlib.sha256(content).hexdigest()
        snapshot = Snapshot(directory, source, hashes)
        snapshot.unchanged()
        yield snapshot
        snapshot.unchanged()


@contextmanager
def snapshot_release(source: Path) -> Iterator[Snapshot]:
    manifest = read_json(source / "release.json")
    validate_manifest(manifest)
    with snapshot_files(
        source, ("release.json", "release.sigstore.json", *manifest["evidence"])
    ) as snapshot:
        # A replacement during enumeration cannot introduce a different evidence set.
        if read_json(snapshot.directory / "release.json") != manifest:
            raise ReleaseError("release artifacts changed during snapshot")
        yield snapshot
