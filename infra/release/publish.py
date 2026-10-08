"""Optional publication after explicit approval, signed evidence and uploaded-byte verification."""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from .build import output
from .gate import release_gate
from .manifest import ReleaseError, digest
from .verify import REPOSITORY, run_checked


def publish(
    candidate: Path, previous: Path, receipt: Path, e2e_sha256: str, *, authorized: bool = False
) -> None:
    if not authorized:
        raise ReleaseError("GitHub release publication requires separate explicit authorization")
    manifest = release_gate(candidate, previous, receipt, e2e_sha256)
    tag = f"release-{manifest['commit']}"
    if output(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"]):
        raise ReleaseError("release tag already exists; refusing to replace audit history")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rollback = root / "previous-release.zip"
        with zipfile.ZipFile(rollback, "x") as archive:
            for name in ("release.json", "release.sigstore.json", *manifest["evidence"]):
                archive.write(previous / name, arcname=name)
        files = [
            candidate / name
            for name in ("release.json", "release.sigstore.json", *manifest["evidence"])
        ]
        files.extend((receipt, receipt.with_suffix(".sigstore.json"), rollback))
        notes = root / "notes.md"
        notes.write_text(
            f"Signed release for commit {manifest['commit']}.\n\n"
            "Manifest, per-image scans/SBOMs, signed staging acceptance, and compatible rollback "
            "artifacts are attached. References #45; this does not close production acceptance.\n",
            encoding="utf-8",
        )
        run_checked(
            [
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                REPOSITORY,
                "--target",
                manifest["commit"],
                "--draft",
                "--title",
                tag,
                "--notes-file",
                str(notes),
                *map(str, files),
            ]
        )
        # No public release until the tag and every uploaded byte are independently read back.
        actual_commit = output(
            ["gh", "api", f"repos/{REPOSITORY}/git/ref/tags/{tag}", "--jq", ".object.sha"]
        )
        if actual_commit != manifest["commit"]:
            raise ReleaseError("draft release tag does not match approved commit")
        downloaded = root / "downloaded"
        downloaded.mkdir()
        run_checked(
            ["gh", "release", "download", tag, "--repo", REPOSITORY, "--dir", str(downloaded)]
        )
        if any(digest(downloaded / path.name) != digest(path) for path in files):
            raise ReleaseError("draft release attachments are absent or mismatched")
        release_gate(candidate, previous, receipt, e2e_sha256)
        run_checked(["gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false"])
