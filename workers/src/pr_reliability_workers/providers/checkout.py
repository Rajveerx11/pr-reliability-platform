"""Credential-safe checkout of an exact GitHub pull request commit."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .github_app import CHECKOUT_PERMISSIONS, GitHubAppInstallationTokenProvider

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_CHECKOUT_NAME = re.compile(r"^pr-review-checkout-[0-9a-f]{64}(?:\.tmp-[A-Za-z0-9_-]+)?$")
_MAX_GIT_OUTPUT_BYTES = 64 * 1024
_ALLOWED_ENVIRONMENT = (
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
)


class GitHubCheckoutError(RuntimeError):
    """Exact checkout failed without exposing credentials or repository output."""


@dataclass(frozen=True, slots=True)
class RepositoryCheckout:
    workspace: Path
    base_sha: str
    head_sha: str
    reference: str


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: str


class ExactHeadCheckout:
    """Create and verify one reusable detached checkout under a trusted root."""

    __slots__ = (
        "_git_executable",
        "_maximum_bytes",
        "_root",
        "_timeout_seconds",
        "_token_provider",
    )

    def __init__(
        self,
        token_provider: GitHubAppInstallationTokenProvider,
        staging_root: Path,
        *,
        git_executable: str = "git",
        timeout_seconds: float = 120.0,
        maximum_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        if not isinstance(token_provider, GitHubAppInstallationTokenProvider):
            raise TypeError("token_provider must be GitHubAppInstallationTokenProvider")
        if not isinstance(staging_root, Path):
            raise TypeError("staging_root must be a Path")
        if not git_executable or "\x00" in git_executable:
            raise ValueError("Git executable is invalid")
        if timeout_seconds <= 0 or timeout_seconds > 900:
            raise ValueError("Git checkout timeout must be between 0 and 900 seconds")
        if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int):
            raise TypeError("maximum_bytes must be an integer")
        if maximum_bytes < 1 or maximum_bytes > 4 * 1024 * 1024 * 1024:
            raise ValueError("Git checkout size limit is invalid")
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = staging_root.resolve(strict=True)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Git checkout root must be a real directory")
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self._token_provider = token_provider
        self._root = root
        self._git_executable = git_executable
        self._timeout_seconds = timeout_seconds
        self._maximum_bytes = maximum_bytes

    async def checkout(
        self,
        repository_full_name: str,
        repository_id: int,
        pull_request_number: int,
        base_sha: str,
        head_sha: str,
        idempotency_key: str,
    ) -> RepositoryCheckout:
        """Return a clean detached checkout exactly matching the requested identity."""

        repository = _validated_repository(repository_full_name)
        _require_positive_integer(repository_id, "GitHub repository ID")
        _require_positive_integer(pull_request_number, "pull request number")
        _require_sha(base_sha, "base SHA")
        _require_sha(head_sha, "head SHA")
        if not idempotency_key or len(idempotency_key) > 512 or "\x00" in idempotency_key:
            raise ValueError("checkout idempotency key is invalid")
        digest = hashlib.sha256(
            f"{repository_id}:{head_sha}:{idempotency_key}".encode()
        ).hexdigest()
        reference = f"checkout:{digest}"
        prefix = f"pr-review-checkout-{digest}"
        target = self._root / prefix
        lock = self._root / f"{prefix}.lock"
        deadline = time.monotonic() + self._timeout_seconds
        await self._acquire_lock(lock, deadline)
        try:
            if target.exists() or target.is_symlink():
                if await self._is_valid(target, repository, base_sha, head_sha, deadline):
                    return RepositoryCheckout(target, base_sha, head_sha, reference)
                _remove_confined(self._root, target)

            token = await self._token_provider.issue(repository_id, CHECKOUT_PERMISSIONS)
            temporary = Path(tempfile.mkdtemp(prefix=f"{prefix}.tmp-", dir=self._root))
            try:
                try:
                    temporary.chmod(0o700)
                except OSError:
                    pass
                await self._run_git(("init", "--quiet"), temporary, deadline)
                await self._run_git(
                    ("remote", "add", "origin", self._repository_url(repository)),
                    temporary,
                    deadline,
                )
                await self._run_git(
                    (
                        "fetch",
                        "--quiet",
                        "--no-tags",
                        "origin",
                        f"+{base_sha}:refs/pr-reliability/base",
                    ),
                    temporary,
                    deadline,
                    token=token.value,
                )
                await self._run_git(
                    (
                        "fetch",
                        "--quiet",
                        "--no-tags",
                        "origin",
                        f"+refs/pull/{pull_request_number}/head:refs/pr-reliability/head",
                    ),
                    temporary,
                    deadline,
                    token=token.value,
                )
                await self._run_git(
                    ("checkout", "--quiet", "--detach", "refs/pr-reliability/head"),
                    temporary,
                    deadline,
                )
                if not await self._is_valid(temporary, repository, base_sha, head_sha, deadline):
                    raise GitHubCheckoutError("GitHub checkout identity verification failed")
                temporary.rename(target)
            except BaseException:
                if temporary.exists() or temporary.is_symlink():
                    _remove_confined(self._root, temporary)
                raise
            return RepositoryCheckout(target, base_sha, head_sha, reference)
        except GitHubCheckoutError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- filesystem and process errors may contain sensitive paths
            raise GitHubCheckoutError("GitHub checkout failed") from None
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass

    async def _is_valid(
        self,
        workspace: Path,
        repository: str,
        base_sha: str,
        head_sha: str,
        deadline: float,
    ) -> bool:
        try:
            if workspace.is_symlink() or not workspace.is_dir():
                return False
            if workspace.resolve(strict=True).parent != self._root:
                return False
            resolved_head = await self._run_git(
                ("rev-parse", "--verify", "HEAD"), workspace, deadline
            )
            if resolved_head.stdout.strip() != head_sha:
                return False
            origin = await self._run_git(("remote", "get-url", "origin"), workspace, deadline)
            if origin.stdout.strip() != self._repository_url(repository):
                return False
            ancestry = await self._run_git(
                ("merge-base", "--is-ancestor", base_sha, head_sha),
                workspace,
                deadline,
                check=False,
            )
            if ancestry.returncode != 0:
                return False
            status = await self._run_git(
                ("status", "--porcelain=v1", "--untracked-files=all"),
                workspace,
                deadline,
            )
            if status.stdout:
                return False
            return await asyncio.to_thread(_workspace_within_limit, workspace, self._maximum_bytes)
        except (GitHubCheckoutError, OSError):
            return False

    async def _run_git(
        self,
        arguments: tuple[str, ...],
        workspace: Path,
        deadline: float,
        *,
        token: str | None = None,
        check: bool = True,
    ) -> _GitResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitHubCheckoutError("GitHub checkout timed out")
        environment = _git_environment(workspace, token)
        creation: dict[str, object] = {}
        if os.name != "nt":
            creation["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(
                self._git_executable,
                *arguments,
                cwd=workspace,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **creation,
            )
        except Exception:  # noqa: BLE001 -- executable errors may contain local details
            raise GitHubCheckoutError("GitHub checkout process failed") from None
        try:
            stdout, _, overflow = await asyncio.wait_for(
                _communicate_bounded(process), timeout=remaining
            )
        except TimeoutError:
            await _stop_process(process)
            raise GitHubCheckoutError("GitHub checkout timed out") from None
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        if overflow:
            raise GitHubCheckoutError("GitHub checkout output limit exceeded")
        if check and process.returncode != 0:
            raise GitHubCheckoutError("GitHub checkout process failed")
        return _GitResult(process.returncode or 0, stdout.decode("utf-8", errors="replace"))

    async def _acquire_lock(self, lock: Path, deadline: float) -> None:
        while True:
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    age = time.time() - lock.stat().st_mtime
                    if age > self._timeout_seconds + 5:
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise GitHubCheckoutError("GitHub checkout timed out")
                await asyncio.sleep(0.05)
                continue
            try:
                os.write(descriptor, str(os.getpid()).encode("ascii"))
            finally:
                os.close(descriptor)
            return

    @staticmethod
    def _repository_url(repository: str) -> str:
        return f"https://github.com/{repository}.git"


async def _communicate_bounded(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes, bool]:
    overflow = False

    async def read(stream: asyncio.StreamReader | None) -> bytes:
        nonlocal overflow
        if stream is None:
            return b""
        kept = bytearray()
        total = 0
        while chunk := await stream.read(8192):
            total += len(chunk)
            if len(kept) < _MAX_GIT_OUTPUT_BYTES:
                kept.extend(chunk[: _MAX_GIT_OUTPUT_BYTES - len(kept)])
            if total > _MAX_GIT_OUTPUT_BYTES:
                overflow = True
                if process.returncode is None:
                    process.kill()
        return bytes(kept)

    stdout, stderr, _ = await asyncio.gather(
        read(process.stdout), read(process.stderr), process.wait()
    )
    return stdout, stderr, overflow


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


def _git_environment(workspace: Path, token: str | None) -> dict[str, str]:
    environment = {
        name: value for name in _ALLOWED_ENVIRONMENT if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(workspace),
            "TEMP": str(workspace),
            "TMP": str(workspace),
            "USERPROFILE": str(workspace),
        }
    )
    if token is not None:
        environment.update(
            {
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
                "GIT_CONFIG_KEY_1": "credential.helper",
                "GIT_CONFIG_VALUE_1": "",
                "GIT_CONFIG_KEY_2": "http.followRedirects",
                "GIT_CONFIG_VALUE_2": "initial",
            }
        )
    return environment


def _workspace_within_limit(workspace: Path, maximum_bytes: int) -> bool:
    total = 0
    for root, directories, files in os.walk(workspace, followlinks=False):
        for name in (*directories, *files):
            path = Path(root, name)
            try:
                total += path.lstat().st_size
            except OSError:
                return False
            if total > maximum_bytes:
                return False
    return True


def _remove_confined(root: Path, path: Path) -> None:
    if path.parent.resolve(strict=True) != root:
        raise GitHubCheckoutError("GitHub checkout cleanup target is invalid")
    if _CHECKOUT_NAME.fullmatch(path.name) is None:
        raise GitHubCheckoutError("GitHub checkout cleanup target is invalid")
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, onexc=_make_writable_then_retry)


def _make_writable_then_retry(function, path: str, error) -> None:
    del error
    os.chmod(path, 0o700)
    function(path)


def _validated_repository(value: str) -> str:
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        raise ValueError("GitHub repository name is invalid")
    owner, name = value.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise ValueError("GitHub repository name is invalid")
    return value


def _require_sha(value: str, subject: str) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{subject} must be a lowercase SHA")


def _require_positive_integer(value: int, subject: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{subject} must be positive")
