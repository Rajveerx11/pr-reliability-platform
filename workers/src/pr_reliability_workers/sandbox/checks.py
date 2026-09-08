"""Strict repository-defined verification check planning."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .models import SandboxLimits, SandboxRequest

CONFIG_FILE_NAME = ".pr-reliability.json"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_CHECKS = 16
_MAX_PATHS = 64
_MAX_TOTAL_CHECK_TIMEOUT_SECONDS = 900
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
_RESOURCE_FIELDS = frozenset(
    {
        "cpu_count",
        "memory_bytes",
        "pids",
        "workspace_bytes",
        "workspace_entries",
        "temp_bytes",
        "output_bytes",
    }
)
_LIMIT_FIELDS = _RESOURCE_FIELDS | {"timeout_seconds"}


class RepositoryCheckConfigError(ValueError):
    """Repository check configuration is absent or unsafe."""

    def __init__(self, code: str) -> None:
        super().__init__(f"repository check configuration is {code}")
        self.code = code


@dataclass(frozen=True)
class ApprovedCheck:
    """Operator-approved image, command, and maximum resource limits."""

    name: str
    image: str
    command: tuple[str, ...]
    maximum_limits: SandboxLimits = field(default_factory=SandboxLimits)

    def __post_init__(self) -> None:
        _validate_name(self.name, ValueError)
        try:
            SandboxRequest(self.image, Path("."), self.command, self.maximum_limits)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"approved check {self.name!r} is invalid") from exc


@dataclass(frozen=True)
class RepositoryCheckPolicy:
    """Trusted operator allowlist indexed by repository check name."""

    checks: tuple[ApprovedCheck, ...]

    def __post_init__(self) -> None:
        if not self.checks or len(self.checks) > _MAX_CHECKS:
            raise ValueError("check allowlist must contain between 1 and 16 checks")
        names = [check.name for check in self.checks]
        if len(set(names)) != len(names):
            raise ValueError("check allowlist names must be unique")

    def get(self, name: str) -> ApprovedCheck | None:
        return next((check for check in self.checks if check.name == name), None)


@dataclass(frozen=True)
class PlannedCheck:
    """One approved check plus its path-filter decision."""

    name: str
    reason_code: Literal["configuration_changed", "path_match", "no_matching_paths"]
    request: SandboxRequest | None


@dataclass(frozen=True)
class VerificationPlan:
    """Checks and Proof of Work input for one exact reviewed checkout."""

    workspace: Path
    checks: tuple[PlannedCheck, ...]
    proof_timeout_seconds: float
    replay_output_ref: str | None = None
    replay_passed: bool | None = None


def parse_check_allowlist(raw: str) -> RepositoryCheckPolicy:
    """Parse trusted operator policy while rejecting unknown or duplicate fields."""

    value = _loads_strict(raw, ValueError)
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_CHECKS:
        raise ValueError("check allowlist must be a list with between 1 and 16 entries")
    checks: list[ApprovedCheck] = []
    for item in value:
        data = _object(item, {"name", "image", "command", "maximum_resources"}, ValueError)
        _require_fields(data, {"name", "image", "command"}, ValueError)
        maximums = _object(data.get("maximum_resources", {}), _LIMIT_FIELDS, ValueError)
        try:
            limits = SandboxLimits(**maximums)
            checks.append(
                ApprovedCheck(
                    name=_name_value(data["name"], ValueError),
                    image=_string(data["image"], ValueError),
                    command=_command(data["command"], ValueError),
                    maximum_limits=limits,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("check allowlist contains an invalid entry") from exc
    return RepositoryCheckPolicy(tuple(checks))


def load_verification_plan(
    workspace: Path,
    changed_paths: tuple[str, ...],
    policy: RepositoryCheckPolicy,
) -> VerificationPlan:
    """Load exact-head repository config and make deterministic run/skip decisions."""

    value = _read_config(workspace / CONFIG_FILE_NAME)
    root = _object(value, {"schema_version", "checks"}, RepositoryCheckConfigError)
    _require_fields(root, {"schema_version", "checks"}, RepositoryCheckConfigError)
    if root["schema_version"] != "1":
        raise RepositoryCheckConfigError("invalid")
    raw_checks = root["checks"]
    if not isinstance(raw_checks, list) or not 1 <= len(raw_checks) <= _MAX_CHECKS:
        raise RepositoryCheckConfigError("invalid")

    planned: list[PlannedCheck] = []
    names: set[str] = set()
    for raw_check in raw_checks:
        request, paths, name = _parse_repository_check(raw_check, workspace, policy)
        if name in names:
            raise RepositoryCheckConfigError("invalid")
        names.add(name)
        if CONFIG_FILE_NAME in changed_paths:
            reason = "configuration_changed"
            selected = True
        else:
            selected = any(
                _path_matches(changed_path, pattern)
                for changed_path in changed_paths
                for pattern in paths
            )
            reason = "path_match" if selected else "no_matching_paths"
        planned.append(PlannedCheck(name, reason, request if selected else None))

    selected_timeouts = [
        item.request.limits.timeout_seconds for item in planned if item.request is not None
    ]
    if sum(selected_timeouts) > _MAX_TOTAL_CHECK_TIMEOUT_SECONDS:
        raise RepositoryCheckConfigError("invalid")
    proof_timeout = max(selected_timeouts, default=SandboxLimits().timeout_seconds)
    return VerificationPlan(workspace, tuple(planned), proof_timeout)


def _parse_repository_check(
    value: object,
    workspace: Path,
    policy: RepositoryCheckPolicy,
) -> tuple[SandboxRequest, tuple[str, ...], str]:
    data = _object(
        value,
        {"name", "image", "command", "paths", "timeout_seconds", "resources"},
        RepositoryCheckConfigError,
    )
    _require_fields(
        data,
        {"name", "image", "command", "paths", "timeout_seconds", "resources"},
        RepositoryCheckConfigError,
    )
    name = _name_value(data["name"], RepositoryCheckConfigError)
    approved = policy.get(name)
    if approved is None:
        raise RepositoryCheckConfigError("invalid")
    image = _string(data["image"], RepositoryCheckConfigError)
    command = _command(data["command"], RepositoryCheckConfigError)
    if image != approved.image or command != approved.command:
        raise RepositoryCheckConfigError("invalid")
    paths = _paths(data["paths"])
    resources = _object(data["resources"], _RESOURCE_FIELDS, RepositoryCheckConfigError)
    _require_fields(resources, _RESOURCE_FIELDS, RepositoryCheckConfigError)
    try:
        limits = SandboxLimits(timeout_seconds=data["timeout_seconds"], **resources)
    except (TypeError, ValueError) as exc:
        raise RepositoryCheckConfigError("invalid") from exc
    _require_within_operator_limits(limits, approved.maximum_limits)
    try:
        request = SandboxRequest(image=image, workspace=workspace, command=command, limits=limits)
    except (TypeError, ValueError) as exc:
        raise RepositoryCheckConfigError("invalid") from exc
    return request, paths, name


def _require_within_operator_limits(actual: SandboxLimits, maximum: SandboxLimits) -> None:
    fields = ("timeout_seconds", *_RESOURCE_FIELDS)
    if any(getattr(actual, name) > getattr(maximum, name) for name in fields):
        raise RepositoryCheckConfigError("invalid")


def _read_config(path: Path) -> object:
    try:
        if path.is_symlink() or not path.is_file():
            raise RepositoryCheckConfigError("missing")
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            raise RepositoryCheckConfigError("invalid")
        raw = path.read_text(encoding="utf-8")
    except RepositoryCheckConfigError:
        raise
    except (OSError, UnicodeError) as exc:
        raise RepositoryCheckConfigError("invalid") from exc
    return _loads_strict(raw, RepositoryCheckConfigError)


def _loads_strict(raw: str, error: type[ValueError]) -> object:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise error("invalid")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=object_pairs)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise error("invalid") from exc


def _object(
    value: object,
    allowed: set[str] | frozenset[str],
    error: type[ValueError],
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise error("invalid")
    if set(value) - set(allowed):
        raise error("invalid")
    return value


def _require_fields(
    value: dict[str, Any], required: set[str] | frozenset[str], error: type[ValueError]
) -> None:
    if not set(required) <= set(value):
        raise error("invalid")


def _name_value(value: object, error: type[ValueError]) -> str:
    name = _string(value, error)
    _validate_name(name, error)
    return name


def _validate_name(name: str, error: type[ValueError]) -> None:
    if _NAME.fullmatch(name) is None:
        raise error("invalid")


def _string(value: object, error: type[ValueError]) -> str:
    if not isinstance(value, str) or not value:
        raise error("invalid")
    return value


def _command(value: object, error: type[ValueError]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise error("invalid")
    command = tuple(value)
    if (
        not command
        or len(command) > 256
        or any(not isinstance(item, str) or not item or "\x00" in item for item in command)
        or sum(len(item) for item in command) > 32_768
    ):
        raise error("invalid")
    return command


def _paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_PATHS:
        raise RepositoryCheckConfigError("invalid")
    paths: list[str] = []
    for pattern in value:
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern) > 256
            or "\x00" in pattern
            or "\\" in pattern
        ):
            raise RepositoryCheckConfigError("invalid")
        path = PurePosixPath(pattern)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in pattern.split("/")):
            raise RepositoryCheckConfigError("invalid")
        paths.append(pattern)
    return tuple(paths)


def _path_matches(path: str, pattern: str) -> bool:
    path_parts = path.split("/")
    pattern_parts = pattern.split("/")
    previous = [False] * (len(path_parts) + 1)
    previous[0] = True
    for part in pattern_parts:
        current = [False] * (len(path_parts) + 1)
        if part == "**":
            current[0] = previous[0]
            for index in range(1, len(current)):
                current[index] = previous[index] or current[index - 1]
        else:
            for index, path_part in enumerate(path_parts, 1):
                current[index] = previous[index - 1] and fnmatch.fnmatchcase(path_part, part)
        previous = current
    return previous[-1]
