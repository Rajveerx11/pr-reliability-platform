"""Consistent PostgreSQL backup and restore for the single-VM deployment."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

DATABASES = ("pr_reliability", "temporal", "temporal_visibility")
RESTORE_CONFIRMATION = "restore-pr-reliability-v1"
_WRITERS = ("api", "command-dispatcher", "workflow-worker", "activity-worker", "temporal")
Runner = Callable[[Sequence[str], BinaryIO | None, BinaryIO | None], int]


class DatabaseOperationError(RuntimeError):
    """Backup or restore could not finish safely."""


def backup(
    repository: Path,
    compose_file: Path,
    environment_file: Path,
    destination: Path,
    *,
    runner: Runner | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Path:
    """Quiesce writers and create one checksummed custom-format dump per database."""
    repository = repository.resolve(strict=True)
    execute = runner or _run
    compose = _compose_command(compose_file, environment_file)
    with _operation_lock(environment_file):
        destination = _external_directory(repository, destination, create=True)
        bundle = destination / now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        bundle.mkdir(mode=0o700)
        resumable_writers = _resumable_writers(execute, compose)
        try:
            _checked(execute, [*compose, "stop", *_WRITERS])
            files: dict[str, str] = {}
            for database_name in DATABASES:
                final_path = bundle / f"{database_name}.dump"
                temporary_path = bundle / f".{database_name}.dump.partial"
                with temporary_path.open("xb") as output:
                    _checked(
                        execute,
                        [
                            *compose,
                            "exec",
                            "-T",
                            "postgres",
                            "pg_dump",
                            "--format=custom",
                            "--no-owner",
                            "--no-privileges",
                            "--username=pr_reliability",
                            f"--dbname={database_name}",
                        ],
                        stdout=output,
                    )
                temporary_path.chmod(0o600)
                temporary_path.replace(final_path)
                files[final_path.name] = _sha256(final_path)
            manifest = bundle / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "created_at": now().astimezone(UTC).isoformat(),
                        "files": files,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest.chmod(0o600)
            return bundle
        finally:
            _resume_writers(execute, compose, resumable_writers)


def restore(
    repository: Path,
    compose_file: Path,
    environment_file: Path,
    bundle: Path,
    confirmation: str,
    *,
    runner: Runner | None = None,
) -> None:
    """Verify and restore all databases while application writers are stopped."""
    if confirmation != RESTORE_CONFIRMATION:
        raise DatabaseOperationError(f"restore requires --confirm {RESTORE_CONFIRMATION}")
    repository = repository.resolve(strict=True)
    execute = runner or _run
    compose = _compose_command(compose_file, environment_file)
    with _operation_lock(environment_file):
        bundle = _external_directory(repository, bundle, create=False)
        dumps = _verified_dumps(bundle)
        resumable_writers = _resumable_writers(execute, compose)
        try:
            _checked(execute, [*compose, "stop", *_WRITERS])
        except BaseException:
            _resume_writers(execute, compose, resumable_writers)
            raise
        for database_name in DATABASES:
            with dumps[database_name].open("rb") as source:
                _checked(
                    execute,
                    [
                        *compose,
                        "exec",
                        "-T",
                        "postgres",
                        "pg_restore",
                        "--clean",
                        "--if-exists",
                        "--exit-on-error",
                        "--single-transaction",
                        "--no-owner",
                        "--no-privileges",
                        "--username=pr_reliability",
                        f"--dbname={database_name}",
                    ],
                    stdin=source,
                )
        _resume_writers(execute, compose, resumable_writers)


@contextlib.contextmanager
def _operation_lock(environment_file: Path) -> Iterator[None]:
    environment_file = environment_file.resolve(strict=True)
    lock_path = environment_file.with_name(".pr-reliability-database.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        if os.name == "posix":
            import fcntl

            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise DatabaseOperationError(
                    "another database backup or restore is running"
                ) from exc
        else:
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise DatabaseOperationError(
                    "another database backup or restore is running"
                ) from exc
        locked = True
        yield
    finally:
        if locked:
            if os.name == "posix":
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _resumable_writers(runner: Runner, compose: Sequence[str]) -> tuple[str, ...]:
    with tempfile.TemporaryFile() as output:
        _checked(
            runner,
            [
                *compose,
                "ps",
                "--status",
                "running",
                "--status",
                "restarting",
                "--services",
            ],
            stdout=output,
        )
        output.seek(0)
        try:
            resumable = set(output.read().decode("utf-8").splitlines())
        except UnicodeDecodeError as exc:
            raise DatabaseOperationError("deployment service state is invalid") from exc
    return tuple(service for service in _WRITERS if service in resumable)


def _resume_writers(runner: Runner, compose: Sequence[str], running_writers: Sequence[str]) -> None:
    if running_writers:
        _checked(runner, [*compose, "start", *running_writers])


def _verified_dumps(bundle: Path) -> dict[str, Path]:
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DatabaseOperationError("backup manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatabaseOperationError("backup manifest is invalid") from exc
    expected_names = {f"{name}.dump" for name in DATABASES}
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != expected_names
    ):
        raise DatabaseOperationError("backup manifest is invalid")
    dumps: dict[str, Path] = {}
    for database_name in DATABASES:
        name = f"{database_name}.dump"
        dump = bundle / name
        if dump.is_symlink() or not dump.is_file() or _sha256(dump) != manifest["files"][name]:
            raise DatabaseOperationError(f"backup checksum failed for {name}")
        dumps[database_name] = dump
    return dumps


def _external_directory(repository: Path, path: Path, *, create: bool) -> Path:
    if not path.is_absolute():
        raise DatabaseOperationError("backup path must be absolute")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DatabaseOperationError("backup path does not exist") from exc
    if path.is_symlink() or not resolved.is_dir() or resolved.is_relative_to(repository):
        raise DatabaseOperationError("backup path must be a real directory outside the repository")
    if os.name == "posix" and resolved.stat().st_mode & 0o077:
        raise DatabaseOperationError("backup directory permissions are too broad")
    return resolved


def _compose_command(compose_file: Path, environment_file: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(environment_file.resolve(strict=True)),
        "--file",
        str(compose_file.resolve(strict=True)),
    ]


def _checked(
    runner: Runner,
    command: Sequence[str],
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> None:
    if runner(command, stdin, stdout) != 0:
        raise DatabaseOperationError(f"deployment command failed: {command[-1]}")


def _run(command: Sequence[str], stdin: BinaryIO | None, stdout: BinaryIO | None) -> int:
    completed = subprocess.run(command, stdin=stdin, stdout=stdout, check=False)
    return completed.returncode


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument(
        "--compose-file", type=Path, default=Path(__file__).with_name("compose.vm.yaml")
    )
    parser.add_argument("--env-file", type=Path, required=True)
    subcommands = parser.add_subparsers(dest="operation", required=True)
    backup_parser = subcommands.add_parser("backup")
    backup_parser.add_argument("destination", type=Path)
    restore_parser = subcommands.add_parser("restore")
    restore_parser.add_argument("bundle", type=Path)
    restore_parser.add_argument("--confirm", required=True)
    arguments = parser.parse_args()
    if arguments.operation == "backup":
        created = backup(
            arguments.repository,
            arguments.compose_file,
            arguments.env_file,
            arguments.destination,
        )
        print(created)
    else:
        restore(
            arguments.repository,
            arguments.compose_file,
            arguments.env_file,
            arguments.bundle,
            arguments.confirm,
        )


if __name__ == "__main__":
    main()
