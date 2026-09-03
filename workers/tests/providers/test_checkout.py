"""Tests for credential-safe exact GitHub checkout."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pr_reliability_workers.providers import (
    CHECKOUT_PERMISSIONS,
    ExactHeadCheckout,
    GitHubAppInstallationTokenProvider,
    GitHubCheckoutError,
    GitHubInstallationToken,
)
from pr_reliability_workers.providers.checkout import _git_environment, _remove_confined

REPOSITORY_ID = 987654
PULL_REQUEST_NUMBER = 7
TOKEN = "ghs_secret_installation_token"


class StaticTokenProvider(GitHubAppInstallationTokenProvider):
    def __init__(self) -> None:
        self.requests: list[tuple[int, dict[str, str]]] = []

    async def issue(self, repository_id: int, permissions):
        self.requests.append((repository_id, dict(permissions)))
        return GitHubInstallationToken(
            TOKEN,
            datetime.now(UTC) + timedelta(hours=1),
            repository_id,
            tuple(sorted(permissions.items())),
        )


class LocalExactHeadCheckout(ExactHeadCheckout):
    def __init__(self, *args, origin: Path, **kwargs) -> None:
        self._origin = origin
        super().__init__(*args, **kwargs)

    def _repository_url(self, repository: str) -> str:
        del repository
        return str(self._origin)


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git(source, "init", "--quiet")
    _git(source, "config", "user.email", "tests@example.com")
    _git(source, "config", "user.name", "Tests")
    (source / "review.py").write_text("BASE = True\n", encoding="utf-8")
    _git(source, "add", "review.py")
    _git(source, "commit", "--quiet", "-m", "base")
    base_sha = _git(source, "rev-parse", "HEAD")
    (source / "review.py").write_text("HEAD = True\n", encoding="utf-8")
    _git(source, "commit", "--quiet", "-am", "head")
    head_sha = _git(source, "rev-parse", "HEAD")
    _git(tmp_path, "clone", "--quiet", "--bare", str(source), str(remote))
    _git(remote, "update-ref", f"refs/pull/{PULL_REQUEST_NUMBER}/head", head_sha)
    return remote, base_sha, head_sha


def test_checkout_is_exact_detached_clean_idempotent_and_token_free(
    tmp_path: Path,
    repository: tuple[Path, str, str],
) -> None:
    remote, base_sha, head_sha = repository
    provider = StaticTokenProvider()
    checkout = LocalExactHeadCheckout(provider, tmp_path / "stage", origin=remote)

    first = asyncio.run(
        checkout.checkout(
            "owner/repository",
            REPOSITORY_ID,
            PULL_REQUEST_NUMBER,
            base_sha,
            head_sha,
            f"run:{head_sha}:select_context",
        )
    )
    second = asyncio.run(
        checkout.checkout(
            "owner/repository",
            REPOSITORY_ID,
            PULL_REQUEST_NUMBER,
            base_sha,
            head_sha,
            f"run:{head_sha}:select_context",
        )
    )

    assert first == second
    assert _git(first.workspace, "rev-parse", "HEAD") == head_sha
    assert _git(first.workspace, "symbolic-ref", "-q", "HEAD", check=False) == ""
    assert _git(first.workspace, "status", "--porcelain=v1") == ""
    assert TOKEN not in (first.workspace / ".git" / "config").read_text(encoding="utf-8")
    assert provider.requests == [(REPOSITORY_ID, CHECKOUT_PERMISSIONS)]
    assert first.reference.startswith("checkout:")
    assert first.workspace.name.startswith("pr-review-checkout-")
    assert not list(first.workspace.parent.glob("pr-review-checkout-*.lock"))


def test_dirty_retry_rebuilds_verified_checkout(
    tmp_path: Path,
    repository: tuple[Path, str, str],
) -> None:
    remote, base_sha, head_sha = repository
    checkout = LocalExactHeadCheckout(StaticTokenProvider(), tmp_path / "stage", origin=remote)
    arguments = (
        "owner/repository",
        REPOSITORY_ID,
        PULL_REQUEST_NUMBER,
        base_sha,
        head_sha,
        f"run:{head_sha}:select_context",
    )
    first = asyncio.run(checkout.checkout(*arguments))
    (first.workspace / "untrusted.txt").write_text("dirty", encoding="utf-8")

    second = asyncio.run(checkout.checkout(*arguments))

    assert second.workspace == first.workspace
    assert not (second.workspace / "untrusted.txt").exists()
    assert _git(second.workspace, "status", "--porcelain=v1") == ""


def test_moved_pull_request_ref_fails_closed_and_cleans_temporary_checkout(
    tmp_path: Path,
    repository: tuple[Path, str, str],
) -> None:
    remote, base_sha, head_sha = repository
    checkout = LocalExactHeadCheckout(StaticTokenProvider(), tmp_path / "stage", origin=remote)
    wrong_head = "f" * 40

    with pytest.raises(GitHubCheckoutError, match="identity verification failed"):
        asyncio.run(
            checkout.checkout(
                "owner/repository",
                REPOSITORY_ID,
                PULL_REQUEST_NUMBER,
                base_sha,
                wrong_head,
                f"run:{wrong_head}:select_context",
            )
        )

    entries = [path.name for path in (tmp_path / "stage").iterdir()]
    assert entries == []
    assert head_sha != wrong_head


def test_git_environment_allowlists_worker_values_and_scopes_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("DATABASE_URL", "database-secret")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", "private-key-secret")

    environment = _git_environment(tmp_path, TOKEN)

    assert "OPENAI_API_KEY" not in environment
    assert "DATABASE_URL" not in environment
    assert "GITHUB_PRIVATE_KEY_PATH" not in environment
    assert TOKEN not in "https://github.com/owner/repository.git"
    assert environment["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert environment["GIT_CONFIG_VALUE_0"] == f"Authorization: Bearer {TOKEN}"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_expired_deadline_never_starts_process(tmp_path: Path) -> None:
    checkout = ExactHeadCheckout(StaticTokenProvider(), tmp_path / "stage")

    with pytest.raises(GitHubCheckoutError, match="timed out"):
        asyncio.run(checkout._run_git(("status",), tmp_path, time.monotonic() - 1))


def test_cleanup_rejects_path_outside_staging_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / ("a" * 64)
    root.mkdir()
    outside.mkdir()

    with pytest.raises(GitHubCheckoutError, match="cleanup target is invalid"):
        _remove_confined(root.resolve(), outside)

    assert outside.exists()


@pytest.mark.parametrize("suffix", ["", ".tmp-test"])
def test_cleanup_accepts_prefixed_checkout_directories(tmp_path: Path, suffix: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    checkout = root / f"pr-review-checkout-{'a' * 64}{suffix}"
    checkout.mkdir()

    _remove_confined(root.resolve(), checkout)

    assert not checkout.exists()


def test_cleanup_rejects_unprefixed_name_inside_staging_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    checkout = root / ("a" * 64)
    checkout.mkdir()

    with pytest.raises(GitHubCheckoutError, match="cleanup target is invalid"):
        _remove_confined(root.resolve(), checkout)

    assert checkout.exists()


def test_process_failure_does_not_expose_executable_or_repository_output(tmp_path: Path) -> None:
    executable = str(tmp_path / "secret-provider-path")
    checkout = ExactHeadCheckout(
        StaticTokenProvider(),
        tmp_path / "stage",
        git_executable=executable,
    )

    with pytest.raises(GitHubCheckoutError) as raised:
        asyncio.run(checkout._run_git(("fetch", "secret-source"), tmp_path, time.monotonic() + 5))

    assert str(raised.value) == "GitHub checkout process failed"
    assert "secret" not in str(raised.value)


def _git(directory: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=directory,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()
