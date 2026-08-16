"""Backup and restore orchestration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from infra.deployment.database import (
    DATABASES,
    RESTORE_CONFIRMATION,
    DatabaseOperationError,
    backup,
    restore,
)


class FakeRunner:
    def __init__(self, *, fail_dump: bool = False, fail_stop: bool = False) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.restored: dict[str, bytes] = {}
        self.fail_dump = fail_dump
        self.fail_stop = fail_stop

    def __call__(self, command, stdin, stdout) -> int:
        values = tuple(command)
        self.commands.append(values)
        if self.fail_stop and "stop" in values:
            return 1
        if "pg_dump" in values:
            if self.fail_dump:
                return 1
            assert stdout is not None
            database_name = values[-1].split("=", 1)[1]
            stdout.write(f"dump:{database_name}".encode())
        if "pg_restore" in values:
            assert stdin is not None
            database_name = values[-1].split("=", 1)[1]
            self.restored[database_name] = stdin.read()
        return 0


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    compose = repository / "compose.yaml"
    compose.touch()
    environment = tmp_path / "deployment.env"
    environment.touch()
    destination = tmp_path / "backups"
    return repository, compose, environment, destination


def test_backup_and_restore_cover_application_and_temporal_databases(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    runner = FakeRunner()
    created = backup(
        repository,
        compose,
        environment,
        destination,
        runner=runner,
        now=lambda: datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
    )

    restore(
        repository,
        compose,
        environment,
        created,
        RESTORE_CONFIRMATION,
        runner=runner,
    )

    assert set(runner.restored) == set(DATABASES)
    assert runner.restored == {name: f"dump:{name}".encode() for name in DATABASES}
    assert runner.commands[0][-6:] == (
        "stop",
        "api",
        "command-dispatcher",
        "workflow-worker",
        "activity-worker",
        "temporal",
    )
    restore_commands = [command for command in runner.commands if "pg_restore" in command]
    assert [command[-1] for command in restore_commands] == [
        f"--dbname={name}" for name in DATABASES
    ]
    assert runner.commands[-1][-2:] == ("up", "-d")


def test_backup_restarts_services_after_dump_failure(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    runner = FakeRunner(fail_dump=True)

    with pytest.raises(DatabaseOperationError, match="deployment command failed"):
        backup(repository, compose, environment, destination, runner=runner)

    assert runner.commands[-1][-2:] == ("up", "-d")


def test_partial_stop_failure_attempts_restart(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    runner = FakeRunner(fail_stop=True)

    with pytest.raises(DatabaseOperationError, match="deployment command failed"):
        backup(repository, compose, environment, destination, runner=runner)

    assert runner.commands[-1][-2:] == ("up", "-d")


def test_restore_rejects_wrong_confirmation_and_modified_dump(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    bundle = backup(repository, compose, environment, destination, runner=FakeRunner())

    with pytest.raises(DatabaseOperationError, match="--confirm"):
        restore(repository, compose, environment, bundle, "wrong", runner=FakeRunner())

    (bundle / "pr_reliability.dump").write_bytes(b"modified")
    with pytest.raises(DatabaseOperationError, match="checksum"):
        restore(repository, compose, environment, bundle, RESTORE_CONFIRMATION, runner=FakeRunner())
