"""Bounded helper process for staging an untrusted repository checkout."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _CopyBudget:
    bytes_left: int
    entries_left: int


def stage_workspace(
    source: Path,
    destination: Path,
    *,
    byte_limit: int,
    entry_limit: int,
) -> None:
    """Copy regular files and symlinks without following links outside the checkout."""

    source = source.resolve(strict=True)
    if not source.is_dir() or destination.exists():
        raise ValueError("invalid staging paths")
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    budget = _CopyBudget(bytes_left=byte_limit, entries_left=entry_limit)
    _copy_directory(source, destination, budget)


def _copy_directory(source: Path, destination: Path, budget: _CopyBudget) -> None:
    with os.scandir(source) as entries:
        for entry in entries:
            if entry.name == ".git":
                continue
            budget.entries_left -= 1
            if budget.entries_left < 0:
                raise ValueError("workspace entry limit exceeded")
            source_path = Path(entry.path)
            destination_path = destination / entry.name
            mode = source_path.lstat().st_mode
            if stat.S_ISLNK(mode):
                destination_path.symlink_to(os.readlink(source_path))
            elif stat.S_ISDIR(mode):
                destination_path.mkdir()
                _copy_directory(source_path, destination_path, budget)
                destination_path.chmod(mode | 0o055)
            elif stat.S_ISREG(mode):
                budget.bytes_left -= source_path.stat().st_size
                if budget.bytes_left < 0:
                    raise ValueError("workspace byte limit exceeded")
                shutil.copyfile(source_path, destination_path, follow_symlinks=False)
                destination_path.chmod(mode | 0o044)
            else:
                raise ValueError("workspace contains a special file")


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit(2)
    try:
        stage_workspace(
            Path(sys.argv[1]),
            Path(sys.argv[2]),
            byte_limit=int(sys.argv[3]),
            entry_limit=int(sys.argv[4]),
        )
    except (OSError, ValueError):
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
