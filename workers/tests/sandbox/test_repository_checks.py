"""Tests for strict repository-defined verification configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pr_reliability_workers.sandbox import (
    CONFIG_FILE_NAME,
    RepositoryCheckConfigError,
    load_verification_plan,
    parse_check_allowlist,
)

IMAGE = f"registry.example/checks@sha256:{'a' * 64}"
OTHER_IMAGE = f"registry.example/checks@sha256:{'b' * 64}"
RESOURCES = {
    "cpu_count": 1,
    "memory_bytes": 128 * 1024 * 1024,
    "pids": 64,
    "workspace_bytes": 64 * 1024 * 1024,
    "workspace_entries": 10_000,
    "temp_bytes": 32 * 1024 * 1024,
    "output_bytes": 64 * 1024,
}


def _policy():
    return parse_check_allowlist(
        json.dumps(
            [
                {
                    "name": "python-tests",
                    "image": IMAGE,
                    "command": ["python", "-m", "pytest", "-q"],
                },
                {
                    "name": "docs-lint",
                    "image": IMAGE,
                    "command": ["python", "-m", "ruff", "check", "docs"],
                },
            ]
        )
    )


def _check(name: str, command: list[str], paths: list[str]) -> dict[str, object]:
    return {
        "name": name,
        "image": IMAGE,
        "command": command,
        "paths": paths,
        "timeout_seconds": 60,
        "resources": RESOURCES,
    }


def _write_config(workspace: Path, checks: list[dict[str, object]]) -> None:
    (workspace / CONFIG_FILE_NAME).write_text(
        json.dumps({"schema_version": "1", "checks": checks}),
        encoding="utf-8",
    )


def test_valid_config_plans_matching_checks_and_records_skip_reason(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        [
            _check("python-tests", ["python", "-m", "pytest", "-q"], ["**/*.py"]),
            _check("docs-lint", ["python", "-m", "ruff", "check", "docs"], ["docs/**"]),
        ],
    )

    plan = load_verification_plan(tmp_path, ("service.py",), _policy())

    assert plan.workspace == tmp_path
    assert [check.name for check in plan.checks] == ["python-tests", "docs-lint"]
    assert plan.checks[0].reason_code == "path_match"
    assert plan.checks[0].request is not None
    assert plan.checks[0].request.limits.memory_bytes == RESOURCES["memory_bytes"]
    assert plan.checks[1].reason_code == "no_matching_paths"
    assert plan.checks[1].request is None


def test_single_star_does_not_cross_directories_and_double_star_does(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        [
            _check("python-tests", ["python", "-m", "pytest", "-q"], ["docs/*.py"]),
            _check("docs-lint", ["python", "-m", "ruff", "check", "docs"], ["docs/**"]),
        ],
    )

    plan = load_verification_plan(tmp_path, ("docs/generated/api.py",), _policy())

    assert plan.checks[0].request is None
    assert plan.checks[1].request is not None


def test_config_change_runs_every_declared_check(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        [_check("python-tests", ["python", "-m", "pytest", "-q"], ["src/**"])],
    )

    plan = load_verification_plan(tmp_path, (CONFIG_FILE_NAME,), _policy())

    assert plan.checks[0].request is not None
    assert plan.checks[0].reason_code == "configuration_changed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: config.update({"unexpected": True}),
        lambda config: config.update({"schema_version": "2"}),
        lambda config: config["checks"][0].update({"command": ["sh", "-c", "env"]}),
        lambda config: config["checks"][0].update({"image": OTHER_IMAGE}),
        lambda config: config["checks"][0].update({"network": "host"}),
        lambda config: config["checks"][0].update({"paths": ["../secrets/**"]}),
        lambda config: config["checks"][0]["resources"].update({"pids": 513}),
    ],
)
def test_unapproved_fields_commands_images_paths_and_limits_fail_closed(
    tmp_path: Path, mutate
) -> None:
    config = {
        "schema_version": "1",
        "checks": [_check("python-tests", ["python", "-m", "pytest", "-q"], ["**/*.py"])],
    }
    mutate(config)
    (tmp_path / CONFIG_FILE_NAME).write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(RepositoryCheckConfigError, match="invalid"):
        load_verification_plan(tmp_path, ("service.py",), _policy())


def test_missing_symlink_duplicate_and_oversized_config_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(RepositoryCheckConfigError, match="missing"):
        load_verification_plan(tmp_path, (), _policy())

    target = tmp_path / "outside.json"
    target.write_text('{"schema_version":"1","checks":[]}', encoding="utf-8")
    config = tmp_path / CONFIG_FILE_NAME
    try:
        config.symlink_to(target)
    except OSError:
        pytest.skip("symlinks require additional privileges")
    with pytest.raises(RepositoryCheckConfigError, match="missing"):
        load_verification_plan(tmp_path, (), _policy())
    config.unlink()

    config.write_text('{"schema_version":"1","schema_version":"1","checks":[]}', encoding="utf-8")
    with pytest.raises(RepositoryCheckConfigError, match="invalid"):
        load_verification_plan(tmp_path, (), _policy())

    config.write_text(" " * (64 * 1024 + 1), encoding="utf-8")
    with pytest.raises(RepositoryCheckConfigError, match="invalid"):
        load_verification_plan(tmp_path, (), _policy())


def test_operator_allowlist_rejects_mutable_images_and_unknown_fields() -> None:
    with pytest.raises(ValueError):
        parse_check_allowlist('[{"name":"tests","image":"python:latest","command":["pytest"]}]')
    with pytest.raises(ValueError):
        parse_check_allowlist(
            f'[{{"name":"tests","image":"{IMAGE}","command":["pytest"],"mount":"/"}}]'
        )


def test_repository_limits_cannot_exceed_lower_operator_maximum(tmp_path: Path) -> None:
    policy = parse_check_allowlist(
        json.dumps(
            [
                {
                    "name": "python-tests",
                    "image": IMAGE,
                    "command": ["python", "-m", "pytest", "-q"],
                    "maximum_resources": {
                        "timeout_seconds": 30,
                        "memory_bytes": 64 * 1024 * 1024,
                    },
                }
            ]
        )
    )
    _write_config(
        tmp_path,
        [_check("python-tests", ["python", "-m", "pytest", "-q"], ["**/*.py"])],
    )

    with pytest.raises(RepositoryCheckConfigError, match="invalid"):
        load_verification_plan(tmp_path, ("service.py",), policy)


def test_selected_checks_have_fifteen_minute_aggregate_timeout_cap(tmp_path: Path) -> None:
    checks = [
        _check("python-tests", ["python", "-m", "pytest", "-q"], ["**/*.py"]),
        _check("docs-lint", ["python", "-m", "ruff", "check", "docs"], ["**/*.py"]),
    ]
    checks[0]["timeout_seconds"] = 451
    checks[1]["timeout_seconds"] = 450
    _write_config(tmp_path, checks)
    policy = parse_check_allowlist(
        json.dumps(
            [
                {
                    "name": "python-tests",
                    "image": IMAGE,
                    "command": ["python", "-m", "pytest", "-q"],
                    "maximum_resources": {"timeout_seconds": 900},
                },
                {
                    "name": "docs-lint",
                    "image": IMAGE,
                    "command": ["python", "-m", "ruff", "check", "docs"],
                    "maximum_resources": {"timeout_seconds": 900},
                },
            ]
        )
    )

    with pytest.raises(RepositoryCheckConfigError, match="invalid"):
        load_verification_plan(tmp_path, ("service.py",), policy)
