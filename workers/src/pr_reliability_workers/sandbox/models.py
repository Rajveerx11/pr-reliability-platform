"""Validated values for disposable command execution."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

_IMMUTABLE_IMAGE = re.compile(
    r"(?:sha256:[0-9a-f]{64}|[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64})\Z"
)
_MAX_TIMEOUT_SECONDS = 900
_MAX_STAGING_TIMEOUT_SECONDS = 120
_MAX_CPU_COUNT = 4
_MAX_MEMORY_BYTES = 4 * 1024 * 1024 * 1024
_MAX_PIDS = 512
_MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TEMP_BYTES = 512 * 1024 * 1024
_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
_MAX_WORKSPACE_ENTRIES = 200_000


@dataclass(frozen=True)
class SandboxLimits:
    """Hard limits applied to one disposable container."""

    timeout_seconds: float = 300
    staging_timeout_seconds: float = 30
    cpu_count: float = 1
    memory_bytes: int = 512 * 1024 * 1024
    pids: int = 128
    workspace_bytes: int = 256 * 1024 * 1024
    workspace_entries: int = 50_000
    temp_bytes: int = 64 * 1024 * 1024
    output_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        if (
            isinstance(self.staging_timeout_seconds, bool)
            or not isinstance(self.staging_timeout_seconds, (int, float))
            or not math.isfinite(self.staging_timeout_seconds)
            or not 0 < self.staging_timeout_seconds <= _MAX_STAGING_TIMEOUT_SECONDS
        ):
            raise ValueError("staging_timeout_seconds must be finite and positive")
        if (
            isinstance(self.cpu_count, bool)
            or not isinstance(self.cpu_count, (int, float))
            or not math.isfinite(self.cpu_count)
            or not 0 < self.cpu_count <= _MAX_CPU_COUNT
        ):
            raise ValueError("cpu_count must be finite and positive")
        for field_name in (
            "memory_bytes",
            "pids",
            "workspace_bytes",
            "workspace_entries",
            "temp_bytes",
            "output_bytes",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.memory_bytes < 6 * 1024 * 1024:
            raise ValueError("memory_bytes must meet Docker's 6 MiB minimum")
        maximums = {
            "memory_bytes": _MAX_MEMORY_BYTES,
            "pids": _MAX_PIDS,
            "workspace_bytes": _MAX_WORKSPACE_BYTES,
            "workspace_entries": _MAX_WORKSPACE_ENTRIES,
            "temp_bytes": _MAX_TEMP_BYTES,
            "output_bytes": _MAX_OUTPUT_BYTES,
        }
        for field_name, maximum in maximums.items():
            if getattr(self, field_name) > maximum:
                raise ValueError(f"{field_name} exceeds the platform maximum")


@dataclass(frozen=True)
class SandboxRequest:
    """One command and reviewed workspace to execute without host shell parsing."""

    image: str
    workspace: Path
    command: tuple[str, ...]
    limits: SandboxLimits = SandboxLimits()

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Path):
            raise TypeError("sandbox workspace must be a Path")
        if not isinstance(self.command, tuple):
            raise TypeError("sandbox command must be an immutable tuple")
        if not isinstance(self.limits, SandboxLimits):
            raise TypeError("sandbox limits must be SandboxLimits")
        if not _IMMUTABLE_IMAGE.fullmatch(self.image):
            raise ValueError("sandbox image must use an immutable sha256 digest")
        if not self.command:
            raise ValueError("sandbox command must not be empty")
        if len(self.command) > 256:
            raise ValueError("sandbox command has too many arguments")
        if any(not isinstance(item, str) for item in self.command):
            raise TypeError("sandbox command arguments must be strings")
        if any(not item or "\x00" in item for item in self.command):
            raise ValueError("sandbox command arguments must be non-empty and contain no NUL")
        if sum(len(item) for item in self.command) > 32_768:
            raise ValueError("sandbox command is too large")


@dataclass(frozen=True)
class SandboxResult:
    """Bounded process evidence returned to the verification adapter."""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    output_limit_exceeded: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.output_limit_exceeded


class SandboxError(RuntimeError):
    """Base class for fail-closed sandbox failures."""


class SandboxUnavailableError(SandboxError):
    """Required Linux container isolation is unavailable."""


class SandboxRuntimeError(SandboxError):
    """The container runtime failed before safe evidence was produced."""


class SandboxCleanupError(SandboxError):
    """The disposable container could not be destroyed."""
