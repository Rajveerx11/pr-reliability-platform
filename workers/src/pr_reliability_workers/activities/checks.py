"""Idempotent, commit-bound GitHub Check Run publishing."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from psycopg import Connection

from ..workflows.types import CheckRunConclusion, CheckRunRequest, CheckRunStatus
from .github_checks import CHECK_RUN_NAME, GitHubCheckRun, GitHubCheckRunClient

_MAX_ANNOTATIONS = 50
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
ConnectionFactory = Callable[[], Connection[Any]]
IdFactory = Callable[[], str]


@dataclass(frozen=True, slots=True)
class _PreparedCheck:
    repository: str
    external_id: str
    details_url: str
    remote_id: int | None
    payload: dict[str, object]
    skip: bool = False


@dataclass
class GitHubCheckRunOperation:
    """Create or update one Check Run per pull request head without duplicates."""

    connection_factory: ConnectionFactory
    client: GitHubCheckRunClient
    dashboard_base_url: str
    id_factory: IdFactory
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        _details_url(self.dashboard_base_url, "01J00000000000000000000000")

    async def __call__(self, request: CheckRunRequest) -> None:
        _validate_request(request)
        claim = await asyncio.to_thread(self._acquire_claim, request)
        try:
            prepared = await asyncio.to_thread(self._prepare, request)
            if prepared.skip:
                return
            remote = None
            if prepared.remote_id is None:
                remote = await self.client.find_check_run(
                    prepared.repository, request.head_sha, prepared.external_id
                )
                if remote is None:
                    remote = await self.client.create_check_run(
                        prepared.repository, prepared.payload
                    )
            else:
                remote = await self.client.update_check_run(
                    prepared.repository, prepared.remote_id, prepared.payload
                )
            if prepared.remote_id is None and remote is not None:
                # Recovery may find a remote check from a prior timed-out create. Patch it to
                # the current desired state before recording the receipt.
                remote = await self.client.update_check_run(
                    prepared.repository, remote.remote_id, prepared.payload
                )
            assert remote is not None
            await asyncio.to_thread(self._record, request, remote)
        except Exception:  # noqa: BLE001 -- provider failures are sanitized for Temporal retries
            raise RuntimeError("GitHub Check Run update failed") from None
        finally:
            await asyncio.to_thread(claim.close)

    def _acquire_claim(self, request: CheckRunRequest) -> Connection[Any]:
        connection = self.connection_factory()
        key = f"github-check:{request.owner_id}:{request.repository_id}:{request.pull_request_number}:{request.head_sha}"
        try:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,)
            ).fetchone()[0]
            if not acquired:
                raise RuntimeError("GitHub Check Run target is busy")
            return connection
        except BaseException:
            connection.close()
            raise

    def _prepare(self, request: CheckRunRequest) -> _PreparedCheck:
        with self.connection_factory() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT run.id, run.generation, pull_request.id, pull_request.public_id,
                       pull_request.head_sha, repository.full_name
                FROM runs AS run
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id AND pull_request.owner_id = run.owner_id
                JOIN repositories AS repository
                  ON repository.id = pull_request.repository_id
                 AND repository.owner_id = run.owner_id
                WHERE run.owner_id = %s AND run.public_id = %s
                  AND repository.public_id = %s AND pull_request.github_number = %s
                  AND run.head_sha = %s
                """,
                (
                    request.owner_id,
                    request.run_id,
                    request.repository_id,
                    request.pull_request_number,
                    request.head_sha,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("Check Run target does not match review run")
            run_id, generation, pull_request_id, pull_request_public_id, _, repository = row
            if generation != request.generation:
                raise RuntimeError("Check Run generation does not match review run")
            external_id = f"pr-reliability:{pull_request_public_id}:{request.head_sha}"
            existing = connection.execute(
                """
                SELECT check_run.current_run_id, current.generation, check_run.remote_id,
                       check_run.status, check_run.conclusion
                FROM github_check_runs AS check_run
                JOIN runs AS current
                  ON current.id = check_run.current_run_id
                 AND current.owner_id = check_run.owner_id
                WHERE check_run.owner_id = %s AND check_run.pull_request_id = %s
                  AND check_run.head_sha = %s AND check_run.check_name = %s
                FOR UPDATE OF check_run
                """,
                (request.owner_id, pull_request_id, request.head_sha, CHECK_RUN_NAME),
            ).fetchone()
            if existing is None:
                if request.status is not CheckRunStatus.QUEUED:
                    raise RuntimeError("Check Run must begin queued")
                connection.execute(
                    """
                    INSERT INTO github_check_runs (
                        public_id, owner_id, pull_request_id, current_run_id, head_sha,
                        check_name, external_id, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued')
                    """,
                    (
                        self.id_factory(),
                        request.owner_id,
                        pull_request_id,
                        run_id,
                        request.head_sha,
                        CHECK_RUN_NAME,
                        external_id,
                    ),
                )
                remote_id = None
                current_status = None
                current_conclusion = None
            else:
                (
                    current_run_id,
                    current_generation,
                    remote_id,
                    current_status,
                    current_conclusion,
                ) = existing
                if request.generation < current_generation:
                    return _PreparedCheck(repository, external_id, "", remote_id, {}, skip=True)
                if request.generation > current_generation:
                    if request.status is not CheckRunStatus.QUEUED:
                        raise RuntimeError("new Check Run generation must begin queued")
                    current_status = None
                    current_conclusion = None
                elif current_run_id != run_id:
                    raise RuntimeError("Check Run generation belongs to another review run")
                elif (
                    remote_id is not None
                    and _already_applied(
                        current_status, current_conclusion, request.status, request.conclusion
                    )
                    or _status_rank(request.status.value) < _status_rank(current_status)
                ):
                    return _PreparedCheck(repository, external_id, "", remote_id, {}, skip=True)

            details_url = _details_url(self.dashboard_base_url, request.run_id)
            annotations = (
                _annotations(connection, request.owner_id, run_id)
                if request.conclusion is CheckRunConclusion.SUCCESS
                else []
            )
            payload = _payload(request, external_id, details_url, annotations, self.now())
            return _PreparedCheck(repository, external_id, details_url, remote_id, payload)

    def _record(self, request: CheckRunRequest, remote: GitHubCheckRun) -> None:
        if remote.head_sha != request.head_sha:
            raise RuntimeError("GitHub Check Run targets another commit")
        with self.connection_factory() as connection, connection.transaction():
            result = connection.execute(
                """
                UPDATE github_check_runs AS check_run
                SET current_run_id = run.id, remote_id = %s, status = %s, conclusion = %s,
                    started_at = CASE
                        WHEN %s = 'in_progress' THEN COALESCE(started_at, %s)
                        WHEN %s = 'queued' THEN NULL ELSE started_at END,
                    completed_at = CASE WHEN %s = 'completed' THEN %s ELSE NULL END,
                    updated_at = now()
                FROM runs AS run, runs AS prior
                WHERE check_run.owner_id = %s AND run.public_id = %s
                  AND run.generation = %s AND check_run.head_sha = %s
                  AND check_run.external_id = %s
                  AND prior.id = check_run.current_run_id
                  AND prior.owner_id = check_run.owner_id
                  AND (
                      check_run.current_run_id = run.id
                      OR (%s = 'queued' AND prior.generation < run.generation)
                  )
                RETURNING check_run.id
                """,
                (
                    remote.remote_id,
                    request.status.value,
                    request.conclusion.value if request.conclusion is not None else None,
                    request.status.value,
                    self.now(),
                    request.status.value,
                    request.status.value,
                    self.now(),
                    request.owner_id,
                    request.run_id,
                    request.generation,
                    request.head_sha,
                    remote.external_id,
                    request.status.value,
                ),
            ).fetchone()
            if result is None:
                raise RuntimeError("Check Run changed before its update was recorded")


def _payload(
    request: CheckRunRequest,
    external_id: str,
    details_url: str,
    annotations: list[dict[str, object]],
    now: datetime,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": CHECK_RUN_NAME,
        "head_sha": request.head_sha,
        "external_id": external_id,
        "details_url": details_url,
        "status": request.status.value,
        "output": {
            "title": _title(request),
            "summary": _summary(request),
            "text": "Open the private dashboard for full evidence and approval history.",
        },
    }
    if request.status is CheckRunStatus.IN_PROGRESS:
        payload["started_at"] = _github_time(now)
    if request.status is CheckRunStatus.COMPLETED:
        assert request.conclusion is not None
        payload["conclusion"] = request.conclusion.value
        payload["completed_at"] = _github_time(now)
        payload["actions"] = [
            {
                "label": "Run review again",
                "description": "Start a new review for this commit",
                "identifier": "rerun",
            }
        ]
        if annotations:
            output = payload["output"]
            assert isinstance(output, dict)
            output["annotations"] = annotations
    return payload


def _annotations(
    connection: Connection[Any], owner_id: str, run_id: int
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT finding.severity, finding.claim, finding.evidence
        FROM findings AS finding
        JOIN approvals AS approval
          ON approval.finding_id = finding.id
         AND approval.run_id = finding.run_id
         AND approval.owner_id = finding.owner_id
        WHERE finding.owner_id = %s AND finding.run_id = %s
          AND approval.decision = 'approved'
        ORDER BY finding.id
        """,
        (owner_id, run_id),
    ).fetchall()
    annotations: list[dict[str, object]] = []
    for severity, claim, evidence_items in rows:
        if not isinstance(claim, str) or not isinstance(evidence_items, list):
            continue
        for evidence in evidence_items:
            if not isinstance(evidence, dict) or evidence.get("kind") != "source_location":
                continue
            path = evidence.get("file_path")
            start = evidence.get("start_line")
            end = evidence.get("end_line", start)
            if not _safe_annotation_location(path, start, end):
                continue
            annotations.append(
                {
                    "path": path,
                    "start_line": start,
                    "end_line": end,
                    "annotation_level": _annotation_level(severity),
                    "message": claim[:1_000],
                    "title": "PR Reliability finding",
                }
            )
            break
        if len(annotations) == _MAX_ANNOTATIONS:
            break
    return annotations


def _safe_annotation_location(path: object, start: object, end: object) -> bool:
    if not isinstance(path, str) or not path or len(path) > 255 or "\\" in path:
        return False
    if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
        return False
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 1 <= start <= end
    )


def _annotation_level(severity: object) -> str:
    if severity in {"high", "critical"}:
        return "failure"
    if severity == "medium":
        return "warning"
    return "notice"


def _title(request: CheckRunRequest) -> str:
    if request.status is CheckRunStatus.QUEUED:
        return "Review queued"
    if request.status is CheckRunStatus.IN_PROGRESS:
        return "Review in progress"
    titles = {
        CheckRunConclusion.SUCCESS: "Review completed",
        CheckRunConclusion.FAILURE: "Review failed",
        CheckRunConclusion.ACTION_REQUIRED: "Action required",
        CheckRunConclusion.CANCELLED: "Review cancelled",
        CheckRunConclusion.TIMED_OUT: "Review timed out",
    }
    return titles[request.conclusion]


def _summary(request: CheckRunRequest) -> str:
    sha = request.head_sha[:12]
    if request.status is CheckRunStatus.QUEUED:
        return f"AI review for {sha} is queued."
    if request.status is CheckRunStatus.IN_PROGRESS:
        return f"AI review for {sha} is running."
    summaries = {
        CheckRunConclusion.SUCCESS: "Approved review findings were published.",
        CheckRunConclusion.FAILURE: "Review could not complete successfully.",
        CheckRunConclusion.ACTION_REQUIRED: "Review findings need human action.",
        CheckRunConclusion.CANCELLED: "Review stopped before completion.",
        CheckRunConclusion.TIMED_OUT: "Review exceeded its approval deadline.",
    }
    return summaries[request.conclusion]


def _already_applied(
    current_status: str,
    current_conclusion: str | None,
    requested_status: CheckRunStatus,
    requested_conclusion: CheckRunConclusion | None,
) -> bool:
    return current_status == requested_status.value and current_conclusion == (
        requested_conclusion.value if requested_conclusion is not None else None
    )


def _status_rank(status: str) -> int:
    return {"queued": 0, "in_progress": 1, "completed": 2}[status]


def _details_url(base_url: str, run_id: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("dashboard base URL is invalid")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("dashboard base URL must use HTTPS")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["run"] = run_id
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _validate_request(request: CheckRunRequest) -> None:
    if _ULID.fullmatch(request.owner_id) is None or _ULID.fullmatch(request.run_id) is None:
        raise ValueError("Check Run owner and run IDs must be ULIDs")
    if _ULID.fullmatch(request.repository_id) is None:
        raise ValueError("Check Run repository ID must be a ULID")
    if _SHA.fullmatch(request.head_sha) is None:
        raise ValueError("invalid Git commit SHA")
    if request.generation < 1 or request.pull_request_number < 1:
        raise ValueError("Check Run generation and pull request number must be positive")
    if (request.status is CheckRunStatus.COMPLETED) != (request.conclusion is not None):
        raise ValueError("completed Check Runs require exactly one conclusion")


def _github_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
