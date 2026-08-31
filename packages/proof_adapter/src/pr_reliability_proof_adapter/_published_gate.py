"""Child-process boundary that exclusively imports the published package."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(2)
    repository = Path(sys.argv[1])
    base_ref = sys.argv[2]
    log_path = Path(sys.argv[3])
    try:
        _validate_changeset(repository, base_ref)
        from proofofwork import __version__
        from proofofwork.engine import check

        verdict = check(
            root=str(repository),
            base_ref=base_ref,
            run_tests=False,
            run_mutation=False,
            use_judge=False,
            db_path=str(log_path),
        )
        payload = {
            "passed": verdict.passed,
            "reasons": verdict.reasons,
            "findings": [{"rule": finding.rule} for finding in verdict.findings],
        }
        print(json.dumps({"ok": True, "package_version": __version__, "payload": payload}))
    except Exception:  # noqa: BLE001 - child must convert every package failure to no verdict
        print(json.dumps({"ok": False}))
        raise SystemExit(1) from None


def _validate_changeset(repository: Path, base_ref: str) -> None:
    commit = subprocess.run(
        ["git", "cat-file", "-e", f"{base_ref}^{{commit}}"],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if commit.returncode != 0:
        raise ValueError("proof base commit is unavailable")
    changed = subprocess.run(
        [
            "git",
            "diff",
            "--quiet",
            "--text",
            "--no-ext-diff",
            "--no-textconv",
            base_ref,
            "--",
        ],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if changed.returncode != 1:
        raise ValueError("proof changeset is empty or unavailable")


if __name__ == "__main__":
    main()
