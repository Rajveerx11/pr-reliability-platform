"""PostgreSQL-backed review activity operations with private ephemeral artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pr_reliability_contracts import ReviewCommand
from psycopg import Connection

from ..activities import VerificationEvidence
from ..agents import ReviewAgent
from ..context import select_context
from ..sandbox import SandboxLimits, SandboxRequest
from ..workflows.types import (
    ModelUsage,
    StageRequest,
    StageResult,
    TerminalRequest,
    WorkflowOutcome,
)
from .checkout import RepositoryCheckout

ConnectionFactory = Callable[[], Connection[Any]]
IdFactory = Callable[[], str]
Now = Callable[[], datetime]
_TERMINAL_STATES = {"published", "rejected", "failed", "cancelled"}
_MAX_REPOSITORY_FILES = 50_000
_MAX_REPOSITORY_BYTES = 64 * 1024 * 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024


class CheckoutProvider(Protocol):
    async def checkout(
        self,
        repository_full_name: str,
        repository_id: int,
        pull_request_number: int,
        base_sha: str,
        head_sha: str,
        idempotency_key: str,
    ) -> RepositoryCheckout: ...


@dataclass(frozen=True)
class _Run:
    internal_id: int
    base_sha: str
    head_sha: str
    state: str
    token_budget: int
    repository: str
    github_repository_id: int
    pull_request_number: int
    pull_request_head_sha: str


@dataclass
class ProductionOperations:
    """Concrete context, analysis, verification, and terminal persistence."""

    connection_factory: ConnectionFactory
    checkout: CheckoutProvider
    reviewer: ReviewAgent
    workspace_root: Path
    sandbox_image: str
    sandbox_command: tuple[str, ...]
    id_factory: IdFactory
    now: Now = lambda: datetime.now(UTC)

    async def select_context(self, request: StageRequest) -> StageResult:
        _require_key(request, "select_context")
        run = await asyncio.to_thread(self._load_active_run, request, "selecting_context")
        expected_ref = _reference("context", request)
        replay = await asyncio.to_thread(self._stage_receipt, request, expected_ref)
        if replay is not None and await asyncio.to_thread(self._artifacts_ready, request):
            return replay
        selected = await self._materialize_context(request, run)
        data = {
            "output_ref": expected_ref,
            "selected_files": len(selected.files),
            "excluded_files": len(selected.excluded),
            "selected_tokens": selected.total_tokens,
        }
        return await asyncio.to_thread(self._record_stage, request, data, None)

    async def analyze(self, request: StageRequest) -> StageResult:
        _require_key(request, "analyze")
        _require_input_ref(request, "context")
        expected_ref = _reference("findings", request)
        run = await asyncio.to_thread(self._load_active_run, request, "analyzing")
        replay = await asyncio.to_thread(self._stage_receipt, request, expected_ref)
        if replay is not None:
            return replay
        if not await asyncio.to_thread(self._artifacts_ready, request):
            await self._materialize_context(request, run)
        context_path = self._context_path(request)
        context = await asyncio.to_thread(context_path.read_text, encoding="utf-8")
        command = ReviewCommand(
            schema_version="1",
            public_id=request.run_id,
            owner_id=request.owner_id,
            run_id=request.run_id,
            head_sha=request.head_sha,
            result_public_id=request.run_id,
        )
        result = await asyncio.to_thread(self.reviewer.review, command, context)
        usage = ModelUsage(
            input_tokens=result.usage.prompt_tokens,
            output_tokens=result.usage.completion_tokens,
            cost_usd_micros=result.usage.reported_cost_usd_micros,
            total_tokens=result.usage.total_tokens,
        )
        data = {
            "output_ref": expected_ref,
            "finding_ids": [finding.public_id for finding in result.findings],
            "usage": _usage_data(usage),
        }
        return await asyncio.to_thread(
            self._record_analysis,
            request,
            result.findings,
            data,
            usage,
        )

    async def prepare_verification(self, request: StageRequest) -> SandboxRequest:
        _require_key(request, "verify")
        _require_input_ref(request, "findings")
        run = await asyncio.to_thread(self._load_active_run, request, "verifying")
        if not await asyncio.to_thread(self._checkout_ready, request):
            await self._materialize_context(request, run)
        return SandboxRequest(
            image=self.sandbox_image,
            workspace=self._checkout_path(request),
            command=self.sandbox_command,
            limits=SandboxLimits(),
        )

    async def record_verification(
        self,
        request: StageRequest,
        evidence: VerificationEvidence,
    ) -> StageResult:
        expected_ref = _reference("verification", request)
        passed = evidence.sandbox.succeeded and evidence.proof is not None and evidence.proof.passed
        data: dict[str, object] = {
            "output_ref": expected_ref,
            "sandbox": {
                "exit_code": evidence.sandbox.exit_code,
                "timed_out": evidence.sandbox.timed_out,
                "output_limit_exceeded": evidence.sandbox.output_limit_exceeded,
            },
            "proof": {
                "passed": evidence.proof.passed,
                "version": evidence.proof.version,
                "package_version": evidence.proof.package_version,
                "finding_rules": list(evidence.proof.finding_rules),
            }
            if evidence.proof is not None
            else None,
            "proof_error": "proof_gate_failed" if evidence.proof_error is not None else None,
        }
        result = await asyncio.to_thread(self._record_stage, request, data, None)
        if passed:
            await asyncio.to_thread(self._mark_awaiting_approval, request)
        return result

    async def record_terminal(self, request: TerminalRequest) -> None:
        _require_terminal_key(request)
        event_data = {
            "outcome": request.outcome.value,
            "reason_code": _terminal_reason_code(request),
            "run_duration_ms": request.run_duration_ms,
            "approval_wait_ms": request.approval_wait_ms,
            "usage": _usage_data(request.usage),
        }
        await asyncio.to_thread(self._record_terminal, request, event_data)
        await asyncio.to_thread(self._remove_artifacts, request)

    async def _materialize_context(self, request: StageRequest, run: _Run):
        run_directory = self._run_directory(request)
        context_path = run_directory / "context.txt"
        if run_directory.exists() or run_directory.is_symlink():
            await asyncio.to_thread(self._remove_checkout, request)
            await asyncio.to_thread(shutil.rmtree, run_directory)
        run_directory.mkdir(mode=0o700, parents=False)
        checked_out = None
        try:
            checked_out = await self.checkout.checkout(
                run.repository,
                run.github_repository_id,
                run.pull_request_number,
                run.base_sha,
                run.head_sha,
                f"{request.run_id}:{request.head_sha}:select_context",
            )
            checkout = checked_out.workspace
            if checked_out.base_sha != run.base_sha or checked_out.head_sha != run.head_sha:
                raise RuntimeError("checkout provider returned another commit")
            _require_private_workspace(self.workspace_root, checkout)
            actual = await _git(checkout, "rev-parse", "--verify", "HEAD^{commit}")
            if actual.strip() != request.head_sha:
                raise RuntimeError("checked-out commit does not match requested head")
            changed = _nul_items(
                await _git_bytes(checkout, "diff", "--name-only", "-z", run.base_sha, run.head_sha)
            )
            tracked = _nul_items(await _git_bytes(checkout, "ls-files", "-z"))
            files = await asyncio.to_thread(_read_text_files, checkout, tracked)
            selected = select_context(files, changed, run.token_budget)
            await asyncio.to_thread(
                _write_private_text, run_directory / "checkout.path", str(checkout)
            )
            await asyncio.to_thread(_write_private_text, context_path, selected.rendered)
            await asyncio.to_thread(self._revalidate_run, request)
            return selected
        except BaseException:
            if checked_out is not None:
                await asyncio.to_thread(
                    _safe_remove_workspace, self.workspace_root, checked_out.workspace
                )
            await asyncio.to_thread(shutil.rmtree, run_directory, True)
            raise

    def _run_directory(self, request: StageRequest | TerminalRequest) -> Path:
        digest = hashlib.sha256(
            f"{request.owner_id}:{request.run_id}:{request.head_sha}".encode()
        ).hexdigest()[:32]
        root = self.workspace_root.resolve(strict=True)
        path = root / f"pr-review-workspace-{digest}"
        if path.parent != root:
            raise RuntimeError("unsafe provider workspace")
        return path

    def _checkout_path(self, request: StageRequest | TerminalRequest) -> Path:
        pointer = self._run_directory(request) / "checkout.path"
        try:
            value = pointer.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("checkout reference is unavailable") from exc
        path = Path(value)
        _require_private_workspace(self.workspace_root, path)
        return path

    def _context_path(self, request: StageRequest) -> Path:
        return self._run_directory(request) / "context.txt"

    def _artifacts_ready(self, request: StageRequest) -> bool:
        return self._checkout_ready(request) and self._context_path(request).is_file()

    def _checkout_ready(self, request: StageRequest) -> bool:
        try:
            checkout = self._checkout_path(request)
        except RuntimeError:
            return False
        if not checkout.is_dir() or checkout.is_symlink():
            return False
        try:
            return (
                _git_sync(checkout, "rev-parse", "--verify", "HEAD^{commit}").strip()
                == request.head_sha
            )
        except RuntimeError:
            return False

    def _remove_artifacts(self, request: TerminalRequest) -> None:
        path = self._run_directory(request)
        self._remove_checkout(request)
        if path.exists():
            shutil.rmtree(path)

    def _remove_checkout(self, request: StageRequest | TerminalRequest) -> None:
        try:
            checkout = self._checkout_path(request)
        except RuntimeError:
            return
        _safe_remove_workspace(self.workspace_root, checkout)

    def _load_active_run(self, request: StageRequest, target_state: str) -> _Run:
        with self.connection_factory() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT run.id, run.base_sha, run.head_sha, run.state, run.token_budget,
                       repository.full_name, repository.github_repository_id,
                       pull_request.github_number, pull_request.head_sha
                FROM runs AS run
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                 AND pull_request.owner_id = run.owner_id
                JOIN repositories AS repository
                  ON repository.id = pull_request.repository_id
                 AND repository.owner_id = run.owner_id
                WHERE run.owner_id = %s AND run.public_id = %s
                FOR UPDATE OF run, pull_request
                """,
                (request.owner_id, request.run_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("review run was not found")
            run = _Run(*row)
            _require_current_run(request, run)
            _require_active_run(run)
            current_rank = _state_rank(run.state)
            target_rank = _state_rank(target_state)
            if current_rank > target_rank:
                return run
            connection.execute(
                """
                UPDATE runs SET state = %s, updated_at = now()
                WHERE id = %s AND owner_id = %s AND head_sha = %s
                """,
                (target_state, run.internal_id, request.owner_id, request.head_sha),
            )
            return run

    def _revalidate_run(self, request: StageRequest) -> None:
        with self.connection_factory() as connection, connection.transaction():
            run = self._locked_run(connection, request)
            _require_current_run(request, run)
            _require_active_run(run)

    def _stage_receipt(
        self,
        request: StageRequest,
        expected_ref: str,
    ) -> StageResult | None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT event.event_data
                FROM run_events AS event
                JOIN runs AS run ON run.id = event.run_id AND run.owner_id = event.owner_id
                WHERE event.owner_id = %s AND run.public_id = %s AND event.event_key = %s
                """,
                (request.owner_id, request.run_id, request.idempotency_key),
            ).fetchone()
        if row is None:
            return None
        data = row[0]
        if not isinstance(data, dict) or data.get("output_ref") != expected_ref:
            raise RuntimeError("activity receipt does not match the request")
        return StageResult(expected_ref, _usage_from_data(data.get("usage")))

    def _record_stage(
        self,
        request: StageRequest,
        event_data: dict[str, object],
        usage: ModelUsage | None,
    ) -> StageResult:
        with self.connection_factory() as connection, connection.transaction():
            run = self._locked_run(connection, request)
            _require_current_run(request, run)
            _require_active_run(run)
            existing = _event_data(connection, run.internal_id, request.idempotency_key)
            if existing is not None:
                if existing != event_data:
                    raise RuntimeError("activity retry result does not match its receipt")
            else:
                _insert_event(
                    connection,
                    self.id_factory(),
                    request.owner_id,
                    run.internal_id,
                    request.idempotency_key,
                    f"activity.{request.idempotency_key.rsplit(':', 1)[-1]}.completed",
                    event_data,
                    self.now(),
                )
        return StageResult(str(event_data["output_ref"]), usage)

    def _record_analysis(
        self,
        request: StageRequest,
        findings,
        event_data: dict[str, object],
        usage: ModelUsage,
    ) -> StageResult:
        with self.connection_factory() as connection, connection.transaction():
            run = self._locked_run(connection, request)
            _require_current_run(request, run)
            _require_active_run(run)
            existing = _event_data(connection, run.internal_id, request.idempotency_key)
            if existing is not None:
                if existing != event_data:
                    raise RuntimeError("analysis retry result does not match its receipt")
                return StageResult(str(existing["output_ref"]), _usage_from_data(existing["usage"]))
            for finding in findings:
                evidence = [item.model_dump(mode="json") for item in finding.evidence]
                connection.execute(
                    """
                    INSERT INTO findings (
                        public_id, owner_id, run_id, finding_key, category,
                        severity, claim, confidence, evidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        finding.public_id,
                        request.owner_id,
                        run.internal_id,
                        finding.public_id,
                        finding.category,
                        finding.severity.value,
                        finding.claim,
                        finding.confidence,
                        json.dumps(evidence),
                    ),
                )
            _insert_event(
                connection,
                self.id_factory(),
                request.owner_id,
                run.internal_id,
                request.idempotency_key,
                "activity.analyze.completed",
                event_data,
                self.now(),
            )
        return StageResult(str(event_data["output_ref"]), usage)

    def _mark_awaiting_approval(self, request: StageRequest) -> None:
        with self.connection_factory() as connection, connection.transaction():
            run = self._locked_run(connection, request)
            _require_current_run(request, run)
            if run.state == "verifying":
                connection.execute(
                    "UPDATE runs SET state = 'awaiting_approval', updated_at = now() WHERE id = %s",
                    (run.internal_id,),
                )
            elif run.state != "awaiting_approval":
                raise RuntimeError("verification cannot advance this run")

    def _record_terminal(self, request: TerminalRequest, event_data: dict[str, object]) -> None:
        state = "failed" if request.outcome is WorkflowOutcome.TIMED_OUT else request.outcome.value
        with self.connection_factory() as connection, connection.transaction():
            run = self._locked_run(connection, request)
            if run.head_sha != request.head_sha:
                raise RuntimeError("terminal request targets another commit")
            existing = _event_data(connection, run.internal_id, request.idempotency_key)
            if existing is not None:
                if existing != event_data:
                    raise RuntimeError("terminal retry does not match its receipt")
                return
            if run.state in _TERMINAL_STATES and run.state != state:
                raise RuntimeError("terminal request conflicts with stored state")
            connection.execute(
                "UPDATE runs SET state = %s, updated_at = now() WHERE id = %s",
                (state, run.internal_id),
            )
            _insert_event(
                connection,
                self.id_factory(),
                request.owner_id,
                run.internal_id,
                request.idempotency_key,
                "run.completed",
                event_data,
                self.now(),
            )

    def _locked_run(self, connection: Connection[Any], request) -> _Run:
        row = connection.execute(
            """
            SELECT run.id, run.base_sha, run.head_sha, run.state, run.token_budget,
                   repository.full_name, repository.github_repository_id,
                   pull_request.github_number, pull_request.head_sha
            FROM runs AS run
            JOIN pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id AND pull_request.owner_id = run.owner_id
            JOIN repositories AS repository
              ON repository.id = pull_request.repository_id AND repository.owner_id = run.owner_id
            WHERE run.owner_id = %s AND run.public_id = %s
            FOR UPDATE OF run, pull_request
            """,
            (request.owner_id, request.run_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("review run was not found")
        return _Run(*row)


def _require_current_run(request: StageRequest, run: _Run) -> None:
    if run.head_sha != request.head_sha or run.pull_request_head_sha != request.head_sha:
        raise RuntimeError("activity targets a stale commit")
    if request.base_sha is not None and request.base_sha != run.base_sha:
        raise RuntimeError("activity base commit does not match the run")


def _require_active_run(run: _Run) -> None:
    if run.state in _TERMINAL_STATES:
        raise RuntimeError("review run is terminal")


def _state_rank(state: str) -> int:
    ranks = {
        "queued": 0,
        "selecting_context": 1,
        "analyzing": 2,
        "verifying": 3,
        "awaiting_approval": 4,
    }
    if state not in ranks:
        raise RuntimeError("review run state is not active")
    return ranks[state]


def _reference(kind: str, request: StageRequest) -> str:
    return f"{kind}:{request.run_id}:{request.head_sha}"


def _require_input_ref(request: StageRequest, kind: str) -> None:
    if request.input_ref != _reference(kind, request):
        raise RuntimeError("activity input reference is invalid")


def _require_key(request: StageRequest, operation: str) -> None:
    if request.idempotency_key != f"{request.run_id}:{request.head_sha}:{operation}":
        raise RuntimeError("activity idempotency key is invalid")


def _require_terminal_key(request: TerminalRequest) -> None:
    expected = f"{request.run_id}:{request.head_sha}:terminal:{request.outcome.value}"
    if request.idempotency_key != expected:
        raise RuntimeError("terminal idempotency key is invalid")


def _terminal_reason_code(request: TerminalRequest) -> str | None:
    if request.reason is None:
        return None
    fixed = {
        "human rejected findings": "human_rejected",
        "approval timeout": "approval_timeout",
        "review activity failed": "review_activity_failed",
        "publish activity failed": "publish_activity_failed",
    }
    if request.reason.startswith("superseded by "):
        return "superseded"
    return fixed.get(request.reason, "cancelled")


def _usage_data(usage: ModelUsage | None) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd_micros": usage.cost_usd_micros,
        "total_tokens": usage.total_tokens,
    }


def _usage_from_data(value: object) -> ModelUsage | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("stored provider usage is invalid")
    return ModelUsage(
        input_tokens=value.get("input_tokens"),
        output_tokens=value.get("output_tokens"),
        cost_usd_micros=value.get("cost_usd_micros"),
        total_tokens=value.get("total_tokens"),
    )


def _event_data(
    connection: Connection[Any],
    run_id: int,
    key: str,
) -> dict[str, object] | None:
    row = connection.execute(
        "SELECT event_data FROM run_events WHERE run_id = %s AND event_key = %s",
        (run_id, key),
    ).fetchone()
    if row is None:
        return None
    if not isinstance(row[0], dict):
        raise TypeError("stored activity receipt is invalid")
    return row[0]


def _insert_event(
    connection: Connection[Any],
    public_id: str,
    owner_id: str,
    run_id: int,
    key: str,
    event_type: str,
    data: dict[str, object],
    occurred_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO run_events (
            public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
        """,
        (
            public_id,
            owner_id,
            run_id,
            key,
            event_type,
            json.dumps(data),
            occurred_at.astimezone(UTC),
        ),
    )


async def _git(repository: Path, *arguments: str) -> str:
    return (await _git_bytes(repository, *arguments)).decode("utf-8", errors="strict")


async def _git_bytes(repository: Path, *arguments: str) -> bytes:
    return await _run_bounded_process(
        "git",
        arguments,
        cwd=repository,
        environment=_git_environment(),
        timeout_seconds=30,
        output_limit_bytes=8 * 1024 * 1024,
    )


async def _run_bounded_process(
    executable: str,
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    output_limit_bytes: int,
) -> bytes:
    if timeout_seconds <= 0 or output_limit_bytes < 1:
        raise ValueError("process limits must be positive")
    creation: dict[str, object] = {}
    if os.name != "nt":
        creation["start_new_session"] = True
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *arguments,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **creation,
        )
    except OSError:
        raise RuntimeError("Git repository inspection failed") from None
    try:
        stdout, overflow = await asyncio.wait_for(
            _capture_bounded(process, output_limit_bytes),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        await _kill_and_reap(process)
        raise RuntimeError("Git repository inspection failed") from None
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(_kill_and_reap(process))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
        raise
    except (OSError, RuntimeError):
        await _kill_and_reap(process)
        raise RuntimeError("Git repository inspection failed") from None
    if process.returncode != 0 or overflow:
        raise RuntimeError("Git repository inspection failed")
    return stdout


async def _capture_bounded(
    process: asyncio.subprocess.Process,
    output_limit_bytes: int,
) -> tuple[bytes, bool]:
    total = 0
    overflow = False
    kept_stdout = bytearray()

    async def read(stream: asyncio.StreamReader | None, *, retain: bool) -> None:
        nonlocal total, overflow
        if stream is None:
            return
        while chunk := await stream.read(8192):
            remaining = max(0, output_limit_bytes - total)
            if retain and remaining:
                kept_stdout.extend(chunk[:remaining])
            total += len(chunk)
            if total > output_limit_bytes and not overflow:
                overflow = True
                _kill_process(process)

    await asyncio.gather(
        read(process.stdout, retain=True),
        read(process.stderr, retain=False),
        process.wait(),
    )
    return bytes(kept_stdout), overflow


def _kill_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    _kill_process(process)
    await process.wait()


def _git_sync(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Git repository inspection failed") from exc
    if result.returncode != 0 or len(result.stdout) > 8 * 1024 * 1024:
        raise RuntimeError("Git repository inspection failed")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Git repository inspection failed") from exc


def _git_environment() -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _nul_items(value: bytes) -> tuple[str, ...]:
    try:
        items = tuple(item.decode("utf-8") for item in value.split(b"\0") if item)
    except UnicodeDecodeError as exc:
        raise RuntimeError("Git returned a non-UTF-8 repository path") from exc
    if len(items) > _MAX_REPOSITORY_FILES:
        raise RuntimeError("repository file count exceeds the provider limit")
    return items


def _read_text_files(repository: Path, paths: tuple[str, ...]) -> dict[str, str]:
    root = repository.resolve(strict=True)
    files: dict[str, str] = {}
    total = 0
    for relative in paths:
        if (
            not relative
            or relative.startswith(("/", "\\"))
            or any(part in {"", ".", ".."} for part in relative.replace("\\", "/").split("/"))
        ):
            raise RuntimeError("Git returned an unsafe repository path")
        path = root.joinpath(*relative.split("/"))
        try:
            mode = path.lstat().st_mode
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(root) or not stat.S_ISREG(mode) or path.is_symlink():
            continue
        size = resolved.stat().st_size
        if size > _MAX_FILE_BYTES or total + size > _MAX_REPOSITORY_BYTES:
            continue
        total += size
        try:
            files[relative] = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return files


def _write_private_text(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def _require_private_workspace(root: Path, workspace: Path) -> Path:
    resolved_root = root.resolve(strict=True)
    try:
        resolved = workspace.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("checkout workspace is unavailable") from exc
    if (
        resolved == resolved_root
        or not resolved.is_relative_to(resolved_root)
        or workspace.is_symlink()
        or not resolved.is_dir()
    ):
        raise RuntimeError("checkout workspace is outside the provider staging root")
    return resolved


def _safe_remove_workspace(root: Path, workspace: Path) -> None:
    resolved = _require_private_workspace(root, workspace)
    shutil.rmtree(resolved)
