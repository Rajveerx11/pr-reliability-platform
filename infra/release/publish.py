"""Optional publication after explicit approval and immutable uploaded-byte verification."""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

from .build import output
from .gate import release_gate
from .manifest import ReleaseError, digest, read_json
from .snapshot import snapshot_files, snapshot_release
from .verify import REPOSITORY, run_checked


def _verify_tag(tag: str, commit: str) -> None:
    reference = json.loads(output(["gh", "api", f"repos/{REPOSITORY}/git/ref/tags/{tag}"]))
    obj = reference.get("object", {})
    # Never dereference an annotated tag or replace a conflicting existing ref.
    if obj.get("type") != "commit" or obj.get("sha") != commit:
        raise ReleaseError("release tag does not match approved commit")


def _draft(tag: str) -> dict | None:
    raw = output(
        [
            "gh",
            "api",
            f"repos/{REPOSITORY}/releases?per_page=100",
            "--paginate",
            "--jq",
            f'.[] | select(.tag_name == "{tag}") | {{id, draft}}',
        ]
    )
    if not raw:
        return None
    try:
        release = json.loads(raw)
        if not isinstance(release, dict) or release.get("draft") is not True:
            raise ValueError
        return release
    except (ValueError, TypeError) as exc:
        raise ReleaseError(
            "existing release is not a single draft; refusing to replace audit history"
        ) from exc


def publish(
    candidate: Path, previous: Path, receipt: Path, e2e_sha256: str, *, authorized: bool = False
) -> None:
    if not authorized:
        raise ReleaseError("GitHub release publication requires separate explicit authorization")
    with (
        snapshot_release(candidate) as candidate_snapshot,
        snapshot_release(previous) as previous_snapshot,
        snapshot_files(
            receipt.parent, (receipt.name, receipt.with_suffix(".sigstore.json").name)
        ) as receipt_snapshot,
        tempfile.TemporaryDirectory() as temporary,
    ):
        candidate = candidate_snapshot.directory
        previous = previous_snapshot.directory
        receipt = receipt_snapshot.directory / receipt.name
        manifest = release_gate(candidate, previous, receipt, e2e_sha256)

        def unchanged() -> None:
            for snapshot in (candidate_snapshot, previous_snapshot, receipt_snapshot):
                snapshot.unchanged()

        unchanged()
        tag = f"release-{manifest['commit']}"
        reference = output(
            [
                "gh",
                "api",
                f"repos/{REPOSITORY}/git/matching-refs/tags/{tag}",
                "--jq",
                f'.[] | select(.ref == "refs/tags/{tag}") | .ref',
            ]
        )
        if not reference:
            # A draft does not create a Git ref. An atomic API create fails on a race;
            # a retry can reuse only the same verified commit, never replace the ref.
            run_checked(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"repos/{REPOSITORY}/git/refs",
                    "-f",
                    f"ref=refs/tags/{tag}",
                    "-f",
                    f"sha={manifest['commit']}",
                ]
            )
        _verify_tag(tag, manifest["commit"])
        existing = _draft(tag)
        root = Path(temporary)
        rollback = root / "previous-release.zip"
        previous_manifest = read_json(previous / "release.json")
        with zipfile.ZipFile(rollback, "x") as archive:
            for name in ("release.json", "release.sigstore.json", *previous_manifest["evidence"]):
                archive.writestr(zipfile.ZipInfo(name), (previous / name).read_bytes())
        files = [
            candidate / name
            for name in ("release.json", "release.sigstore.json", *manifest["evidence"])
        ]
        files.extend((receipt, receipt.with_suffix(".sigstore.json"), rollback))
        expected = {path.name: digest(path) for path in files}
        notes = root / "notes.md"
        notes.write_text(
            f"Signed release for commit {manifest['commit']}.\n\n"
            "Manifest, per-image scans/SBOMs, signed staging acceptance, and compatible rollback "
            "artifacts are attached. References #45; this does not close production acceptance.\n",
            encoding="utf-8",
        )
        if existing is None:
            unchanged()
            run_checked(
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    "--repo",
                    REPOSITORY,
                    "--verify-tag",
                    "--draft",
                    "--title",
                    tag,
                    "--notes-file",
                    str(notes),
                    *map(str, files),
                ]
            )
        # Retry complete drafts by checking existing bytes, not uploading with --clobber.
        downloaded = root / "downloaded"
        downloaded.mkdir()
        run_checked(
            ["gh", "release", "download", tag, "--repo", REPOSITORY, "--dir", str(downloaded)]
        )
        if {path.name for path in downloaded.iterdir()} != set(expected) or any(
            digest(downloaded / name) != checksum for name, checksum in expected.items()
        ):
            raise ReleaseError("draft release attachments are absent or mismatched")
        if release_gate(candidate, previous, receipt, e2e_sha256) != manifest:
            raise ReleaseError("approved release changed during publication")
        _verify_tag(tag, manifest["commit"])
        if _draft(tag) is None:
            raise ReleaseError("verified draft disappeared before publication")
        unchanged()
        run_checked(["gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false"])
