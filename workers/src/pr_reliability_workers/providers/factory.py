"""Environment-backed assembly for the complete production activity set."""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from collections.abc import Callable
from pathlib import Path

import psycopg
from pr_reliability_proof_adapter import ProofAdapter

from ..activities import (
    ActivityOperations,
    GitHubRestReviewClient,
    GitHubReviewPublishOperation,
    SandboxVerificationOperation,
)
from ..agents import ReviewAgent
from ..sandbox import DockerSandboxRunner
from .openai import OpenAIResponsesClient
from .operations import ConnectionFactory, ProductionOperations

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def create_operations() -> ActivityOperations:
    """Build production operations from validated environment values."""

    if _required("MODEL_PROVIDER") != "openai":
        raise RuntimeError("MODEL_PROVIDER must be openai")
    database_url = _required("DATABASE_URL")
    owner_id = _required("OWNER_ID")
    staging_root = _private_directory(Path(_required("SANDBOX_STAGING_DIRECTORY")))
    private_key = _private_key(Path(_required("GITHUB_PRIVATE_KEY_PATH")))
    sandbox_command = _command(_required("REVIEW_SANDBOX_COMMAND_JSON"))

    connection_factory: ConnectionFactory = lambda: psycopg.connect(database_url)
    repository_id_resolver = _repository_id_resolver(connection_factory, owner_id)

    # Imported lazily so module inspection never reads credentials or configures clients.
    from .checkout import ExactHeadCheckout
    from .github_app import GitHubAppInstallationTokenProvider

    tokens = GitHubAppInstallationTokenProvider(
        _positive_int("GITHUB_APP_ID"),
        _positive_int("GITHUB_INSTALLATION_ID"),
        private_key,
        timeout_seconds=_positive_float("GITHUB_API_TIMEOUT_SECONDS", 10.0),
    )
    github = GitHubRestReviewClient(
        tokens,
        _positive_int("GITHUB_APP_BOT_USER_ID"),
        repository_id_resolver=repository_id_resolver,
        timeout_seconds=_positive_float("GITHUB_API_TIMEOUT_SECONDS", 10.0),
    )
    checkout = ExactHeadCheckout(
        tokens,
        staging_root,
        timeout_seconds=_positive_float("GITHUB_CHECKOUT_TIMEOUT_SECONDS", 120.0),
    )
    reviewer = ReviewAgent(
        OpenAIResponsesClient(
            _required("OPENAI_API_KEY"),
            _required("OPENAI_MODEL"),
            max_output_tokens=_positive_int("OPENAI_MAX_OUTPUT_TOKENS", 4_096),
            timeout_seconds=_positive_float("OPENAI_TIMEOUT_SECONDS", 120.0),
        )
    )
    core = ProductionOperations(
        connection_factory=connection_factory,
        checkout=checkout,
        reviewer=reviewer,
        workspace_root=staging_root,
        sandbox_image=_required("REVIEW_SANDBOX_IMAGE"),
        sandbox_command=sandbox_command,
        id_factory=_new_ulid,
    )
    verify = SandboxVerificationOperation(
        prepare=core.prepare_verification,
        runner=DockerSandboxRunner(),
        record=core.record_verification,
        proof=ProofAdapter(),
    )
    publish = GitHubReviewPublishOperation(
        connection_factory=connection_factory,
        client=github,
        id_factory=_new_ulid,
    )
    return ActivityOperations(
        select_context=core.select_context,
        analyze=core.analyze,
        verify=verify,
        publish=publish,
        record_terminal=core.record_terminal,
    )


def _repository_id_resolver(
    connection_factory: ConnectionFactory,
    owner_id: str,
) -> Callable[[str], int]:
    def resolve(repository: str) -> int:
        with connection_factory() as connection:
            row = connection.execute(
                """
                SELECT github_repository_id
                FROM repositories
                WHERE owner_id = %s AND full_name = %s
                """,
                (owner_id, repository),
            ).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 1:
            raise RuntimeError("GitHub repository identity was not found")
        return row[0]

    return resolve


def _private_directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("SANDBOX_STAGING_DIRECTORY must be a real absolute directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("SANDBOX_STAGING_DIRECTORY is unavailable") from exc
    if not resolved.is_dir():
        raise RuntimeError("SANDBOX_STAGING_DIRECTORY must be a directory")
    return resolved


def _private_key(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("GITHUB_PRIVATE_KEY_PATH must be a real absolute file")
    try:
        status = path.stat()
        if not stat.S_ISREG(status.st_mode) or not 0 < status.st_size <= 64 * 1024:
            raise RuntimeError("GITHUB_PRIVATE_KEY_PATH is invalid")
        return path.read_bytes()
    except OSError as exc:
        raise RuntimeError("GITHUB_PRIVATE_KEY_PATH is unavailable") from exc


def _command(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("REVIEW_SANDBOX_COMMAND_JSON must be valid JSON") from exc
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 256
        or any(not isinstance(item, str) or not item or "\x00" in item for item in value)
    ):
        raise RuntimeError("REVIEW_SANDBOX_COMMAND_JSON must be a non-empty argument list")
    return tuple(value)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def _positive_int(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None and default is not None:
        return default
    try:
        value = int(raw or "")
    except ValueError as exc:
        raise RuntimeError(f"{name} must be positive") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be positive")
    return value


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be positive") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _new_ulid() -> str:
    value = (time.time_ns() // 1_000_000 << 80) | secrets.randbits(80)
    encoded = ""
    for _ in range(26):
        encoded = _CROCKFORD[value & 31] + encoded
        value >>= 5
    return encoded
