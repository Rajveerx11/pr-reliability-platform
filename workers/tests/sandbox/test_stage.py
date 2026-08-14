"""Tests for bounded host-side workspace staging."""

import os
import stat
from pathlib import Path

import pytest
from pr_reliability_workers.sandbox.stage import stage_workspace


def test_staging_excludes_git_and_preserves_only_bounded_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("credential", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "code.py").write_text("print('ok')", encoding="utf-8")

    stage_workspace(source, destination, byte_limit=1024, entry_limit=2)

    assert (destination / "nested" / "code.py").read_text(encoding="utf-8") == "print('ok')"
    assert not (destination / ".git").exists()


def test_staging_rejects_entry_and_byte_exhaustion(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one").write_text("1", encoding="utf-8")
    (source / "two").write_text("22", encoding="utf-8")

    with pytest.raises(ValueError, match="entry limit"):
        stage_workspace(
            source,
            tmp_path / "entry-destination",
            byte_limit=1024,
            entry_limit=1,
        )
    with pytest.raises(ValueError, match="byte limit"):
        stage_workspace(
            source,
            tmp_path / "byte-destination",
            byte_limit=2,
            entry_limit=10,
        )


def test_staged_root_remains_container_readable_under_hardened_umask(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    prior_umask = os.umask(0o077)
    try:
        stage_workspace(source, destination, byte_limit=1024, entry_limit=10)
    finally:
        os.umask(prior_umask)

    root_mode = destination.stat().st_mode
    assert root_mode & stat.S_IROTH
    assert root_mode & stat.S_IXOTH
