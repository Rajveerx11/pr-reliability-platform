"""Backup and restore orchestration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from infra.deployment.database import (
    DATABASES,
    RESTORE_CONFIRMATION,
    DatabaseOperationError,
    _operation_lock,
    backup,
    restore,
)


class FakeRunner:
    def __init__(
        self,
        *,
        fail_dump: bool = False,
        fail_restore: bool = False,
        fail_stop: bool = False,
        running_services: tuple[str, ...] = (
            "api",
            "command-dispatcher",
            "workflow-worker",
            "activity-worker",
            "temporal",
        ),
        restarting_services: tuple[str, ...] = (),
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.restored: dict[str, bytes] = {}
        self.fail_dump = fail_dump
        self.fail_restore = fail_restore
        self.fail_stop = fail_stop
        self.running_services = running_services
        self.restarting_services = restarting_services

    def __call__(self, command, stdin, stdout) -> int:
        values = tuple(command)
        self.commands.append(values)
        if "ps" in values:
            assert stdout is not None
            services = (*self.running_services, *self.restarting_services)
            stdout.write(("\n".join(services) + "\n").encode())
        if self.fail_stop and "stop" in values:
            return 1
        if "pg_dump" in values:
            if self.fail_dump:
                return 1
            assert stdout is not None
            database_name = values[-1].split("=", 1)[1]
            stdout.write(f"dump:{database_name}".encode())
        if "pg_restore" in values:
            if self.fail_restore:
                return 1
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
        "ps",
        "--status",
        "running",
        "--status",
        "restarting",
        "--services",
    )
    assert runner.commands[1][-6:] == (
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
    dump_commands = [command for command in runner.commands if "pg_dump" in command]
    assert all("--username=backup_operator" in command for command in dump_commands)
    assert all("--username=backup_operator" in command for command in restore_commands)
    assert [
        next(value for value in command if value.startswith("--role=")) for command in dump_commands
    ] == [
        "--role=pr_reliability",
        "--role=temporal",
        "--role=temporal",
    ]
    assert runner.commands[-1][-6:] == (
        "start",
        "api",
        "command-dispatcher",
        "workflow-worker",
        "activity-worker",
        "temporal",
    )


def test_backup_restarts_services_after_dump_failure(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    runner = FakeRunner(fail_dump=True)

    with pytest.raises(DatabaseOperationError, match="deployment command failed"):
        backup(repository, compose, environment, destination, runner=runner)

    assert runner.commands[-1][-6:] == (
        "start",
        "api",
        "command-dispatcher",
        "workflow-worker",
        "activity-worker",
        "temporal",
    )


def test_partial_stop_failure_attempts_restart(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    runner = FakeRunner(fail_stop=True)

    with pytest.raises(DatabaseOperationError, match="deployment command failed"):
        backup(repository, compose, environment, destination, runner=runner)

    assert runner.commands[-1][-6:] == (
        "start",
        "api",
        "command-dispatcher",
        "workflow-worker",
        "activity-worker",
        "temporal",
    )


def test_backup_preserves_intentionally_stopped_writer_services(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    runner = FakeRunner(running_services=("api", "workflow-worker", "temporal"))

    backup(repository, compose, environment, destination, runner=runner)

    start_commands = [command for command in runner.commands if "start" in command]
    assert start_commands == [
        (
            *runner.commands[0][:-6],
            "start",
            "api",
            "workflow-worker",
            "temporal",
        )
    ]
    assert all("command-dispatcher" not in command for command in start_commands)
    assert all("activity-worker" not in command for command in start_commands)
    assert not any("up" in command for command in runner.commands)


def test_backup_resumes_writer_observed_while_restarting(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    runner = FakeRunner(
        running_services=("api",),
        restarting_services=("command-dispatcher",),
    )

    backup(repository, compose, environment, destination, runner=runner)

    assert runner.commands[0][-6:] == (
        "ps",
        "--status",
        "running",
        "--status",
        "restarting",
        "--services",
    )
    start_commands = [command for command in runner.commands if "start" in command]
    assert start_commands[0][-3:] == ("start", "api", "command-dispatcher")
    assert all("workflow-worker" not in command for command in start_commands)


def test_failed_restore_leaves_writers_stopped_for_operator_recovery(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    bundle = backup(repository, compose, environment, destination, runner=FakeRunner())
    runner = FakeRunner(
        fail_restore=True,
        running_services=("api", "workflow-worker", "temporal"),
    )

    with pytest.raises(DatabaseOperationError, match="deployment command failed"):
        restore(repository, compose, environment, bundle, RESTORE_CONFIRMATION, runner=runner)

    assert not any("start" in command for command in runner.commands)
    assert not any("up" in command for command in runner.commands)


def test_successful_restore_preserves_intentionally_stopped_writer_services(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    bundle = backup(repository, compose, environment, destination, runner=FakeRunner())
    runner = FakeRunner(running_services=("api", "workflow-worker", "temporal"))

    restore(repository, compose, environment, bundle, RESTORE_CONFIRMATION, runner=runner)

    start_commands = [command for command in runner.commands if "start" in command]
    assert start_commands[0][-4:] == ("start", "api", "workflow-worker", "temporal")
    assert all("command-dispatcher" not in command for command in start_commands)
    assert all("activity-worker" not in command for command in start_commands)
    assert not any("up" in command for command in runner.commands)


def test_restore_stop_failure_restores_only_initially_running_writers(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    bundle = backup(repository, compose, environment, destination, runner=FakeRunner())
    runner = FakeRunner(
        fail_stop=True,
        running_services=("api", "workflow-worker", "temporal"),
    )

    with pytest.raises(DatabaseOperationError, match="deployment command failed"):
        restore(repository, compose, environment, bundle, RESTORE_CONFIRMATION, runner=runner)

    start_commands = [command for command in runner.commands if "start" in command]
    assert start_commands[0][-4:] == ("start", "api", "workflow-worker", "temporal")
    assert all("command-dispatcher" not in command for command in start_commands)
    assert all("activity-worker" not in command for command in start_commands)


def test_operation_lock_rejects_overlapping_backup_and_restore_without_mutation(
    tmp_path: Path,
) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    bundle = backup(repository, compose, environment, destination, runner=FakeRunner())
    blocked_destination = tmp_path / "blocked-backups"
    runner = FakeRunner()

    with _operation_lock(environment):
        with pytest.raises(DatabaseOperationError, match="another database backup or restore"):
            backup(repository, compose, environment, blocked_destination, runner=runner)
        with pytest.raises(DatabaseOperationError, match="another database backup or restore"):
            restore(
                repository,
                compose,
                environment,
                bundle,
                RESTORE_CONFIRMATION,
                runner=runner,
            )

    assert runner.commands == []
    assert not blocked_destination.exists()


def test_restore_rejects_wrong_confirmation_and_modified_dump(tmp_path: Path) -> None:
    repository, compose, environment, destination = _paths(tmp_path)
    bundle = backup(repository, compose, environment, destination, runner=FakeRunner())

    with pytest.raises(DatabaseOperationError, match="--confirm"):
        restore(repository, compose, environment, bundle, "wrong", runner=FakeRunner())

    (bundle / "pr_reliability.dump").write_bytes(b"modified")
    with pytest.raises(DatabaseOperationError, match="checksum"):
        restore(repository, compose, environment, bundle, RESTORE_CONFIRMATION, runner=FakeRunner())
