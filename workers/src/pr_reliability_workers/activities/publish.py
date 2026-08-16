"""Approval-bound, idempotent GitHub comment publishing."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg import Connection
from temporalio.exceptions import ApplicationError

from ..workflows.types import PublishRequest

ConnectionFactory = Callable[[], Connection[Any]]
IdFactory = Callable[[], str]
Now = Callable[[], datetime]
_MAX_COMMENT_CHARACTERS = 60_000


@dataclass(frozen=True)
class GitHubComment:
    """Minimum remote comment identity needed for retry recovery."""

    remote_id: str

    def __post_init__(self) -> None:
        if not self.remote_id or len(self.remote_id) > 128 or not self.remote_id.isascii():
            raise ValueError("GitHub comment ID must be bounded ASCII text")


class GitHubCommentClient(Protocol):
    """Repository-scoped GitHub operations supplied by the provider worker.

    ``find_comment`` must return only comments authored by the authenticated
    GitHub App identity. The marker is an idempotency aid, not an authorization
    secret.
    """

    async def current_head_sha(self, repository: str, pull_request_number: int) -> str: ...

    async def find_comment(
        self,
        repository: str,
        pull_request_number: int,
        marker: str,
    ) -> GitHubComment | None: ...

    async def create_comment(
        self,
        repository: str,
        pull_request_number: int,
        body: str,
    ) -> GitHubComment: ...


class PublishBlockedError(RuntimeError):
    """Requested external write is not authorized by current database state."""


@dataclass(frozen=True)
class _PreparedPublish:
    action_id: str
    repository: str
    pull_request_number: int
    head_sha: str
    marker: str
    body: str
    remote_id: str | None = None


@dataclass(frozen=True)
class GitHubCommentPublishOperation:
    """Publish approved findings once and record only bounded audit facts."""

    connection_factory: ConnectionFactory
    client: GitHubCommentClient
    id_factory: IdFactory
    now: Now = lambda: datetime.now(UTC)

    async def __call__(self, request: PublishRequest) -> None:
        try:
            prepared = await asyncio.to_thread(self._prepare, request)
        except PublishBlockedError as exc:
            raise _blocked_application_error(exc) from exc
        if prepared.remote_id is not None:
            return

        try:
            comment = await self.client.find_comment(
                prepared.repository,
                prepared.pull_request_number,
                prepared.marker,
            )
            if comment is None:
                remote_head = await self.client.current_head_sha(
                    prepared.repository,
                    prepared.pull_request_number,
                )
                if remote_head != prepared.head_sha:
                    await asyncio.to_thread(
                        self._record_failure,
                        request,
                        prepared.action_id,
                        "stale_head",
                    )
                    raise ApplicationError(
                        "pull request head is stale",
                        type="PublishBlocked",
                        non_retryable=True,
                    )
                comment = await self.client.create_comment(
                    prepared.repository,
                    prepared.pull_request_number,
                    prepared.body,
                )
            await asyncio.to_thread(self._record_success, request, prepared, comment)
        except ApplicationError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self._record_failure,
                request,
                prepared.action_id,
                "github_error",
            )
            raise RuntimeError("GitHub comment publish failed") from exc

    def _prepare(self, request: PublishRequest) -> _PreparedPublish:
        _validate_request_shape(request)
        with self.connection_factory() as connection, connection.transaction():
            run = connection.execute(
                """
                SELECT run.id, run.state, pull_request.head_sha, repository.full_name
                FROM runs AS run
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                 AND pull_request.owner_id = run.owner_id
                JOIN repositories AS repository
                  ON repository.id = pull_request.repository_id
                 AND repository.owner_id = run.owner_id
                WHERE run.owner_id = %s
                  AND run.public_id = %s
                  AND repository.public_id = %s
                  AND pull_request.github_number = %s
                FOR UPDATE OF run, pull_request
                """,
                (
                    request.owner_id,
                    request.run_id,
                    request.repository_id,
                    request.pull_request_number,
                ),
            ).fetchone()
            if run is None:
                raise PublishBlockedError("publish target does not match the run")
            internal_run_id, run_state, current_head_sha, repository = run

            existing = connection.execute(
                """
                SELECT public_id, status, remote_id, target_sha, run_id, action_type
                FROM external_actions
                WHERE owner_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (request.owner_id, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                action_id, action_status, remote_id, target_sha, action_run_id, action_type = (
                    existing
                )
                if target_sha != request.head_sha:
                    raise PublishBlockedError("idempotency key targets another commit")
                if action_run_id != internal_run_id or action_type != "github.pull_request_comment":
                    raise PublishBlockedError("idempotency key targets another action")
                if action_status == "published":
                    if remote_id is None:
                        raise PublishBlockedError("published action has no remote identity")
                    return _PreparedPublish(
                        action_id,
                        repository,
                        request.pull_request_number,
                        request.head_sha,
                        _comment_marker(request.idempotency_key),
                        "",
                        remote_id,
                    )

            if run_state != "awaiting_approval":
                raise PublishBlockedError("run is not awaiting approval")
            if current_head_sha != request.head_sha:
                raise PublishBlockedError("pull request head is stale")

            findings = connection.execute(
                """
                SELECT finding.public_id, finding.severity, finding.claim,
                       approval.public_id, approval.decision, approval.head_sha
                FROM findings AS finding
                JOIN approvals AS approval
                  ON approval.finding_id = finding.id
                 AND approval.run_id = finding.run_id
                 AND approval.owner_id = finding.owner_id
                WHERE finding.owner_id = %s
                  AND finding.run_id = %s
                  AND finding.public_id = ANY(%s)
                  AND approval.public_id = ANY(%s)
                ORDER BY finding.id
                """,
                (
                    request.owner_id,
                    internal_run_id,
                    list(request.finding_ids),
                    list(request.approval_ids),
                ),
            ).fetchall()
            if len(findings) != len(request.finding_ids):
                raise PublishBlockedError("every finding needs its matching approval")
            if any(decision != "approved" for *_, decision, _ in findings):
                raise PublishBlockedError("rejected findings cannot publish")
            if any(head_sha != request.head_sha for *_, head_sha in findings):
                raise PublishBlockedError("approval targets another commit")
            returned_approvals = {row[3] for row in findings}
            if returned_approvals != set(request.approval_ids):
                raise PublishBlockedError("approval set does not match finding set")

            marker = _comment_marker(request.idempotency_key)
            body = _render_comment(findings, marker)
            if existing is None:
                action_id = self.id_factory()
                connection.execute(
                    """
                    INSERT INTO external_actions (
                        public_id, owner_id, run_id, action_type, target_sha,
                        idempotency_key, status
                    ) VALUES (%s, %s, %s, 'github.pull_request_comment', %s, %s, 'publishing')
                    """,
                    (
                        action_id,
                        request.owner_id,
                        internal_run_id,
                        request.head_sha,
                        request.idempotency_key,
                    ),
                )
            else:
                action_id = existing[0]
                connection.execute(
                    """
                    UPDATE external_actions
                    SET status = 'publishing', updated_at = now()
                    WHERE owner_id = %s AND public_id = %s
                    """,
                    (request.owner_id, action_id),
                )
        return _PreparedPublish(
            action_id,
            repository,
            request.pull_request_number,
            request.head_sha,
            marker,
            body,
        )

    def _record_success(
        self,
        request: PublishRequest,
        prepared: _PreparedPublish,
        comment: GitHubComment,
    ) -> None:
        occurred_at = self.now().astimezone(UTC)
        with self.connection_factory() as connection, connection.transaction():
            action = connection.execute(
                """
                UPDATE external_actions AS action
                SET status = 'published', remote_id = %s, updated_at = now()
                FROM runs AS run
                WHERE action.run_id = run.id
                  AND action.owner_id = run.owner_id
                  AND action.owner_id = %s
                  AND action.public_id = %s
                  AND run.public_id = %s
                  AND action.target_sha = %s
                RETURNING action.run_id
                """,
                (
                    comment.remote_id,
                    request.owner_id,
                    prepared.action_id,
                    request.run_id,
                    request.head_sha,
                ),
            ).fetchone()
            if action is None:
                raise RuntimeError("publish action disappeared before audit")
            connection.execute(
                """
                INSERT INTO run_events (
                    public_id, owner_id, run_id, event_key, event_type,
                    event_data, occurred_at
                ) VALUES (%s, %s, %s, %s, 'github.comment_published', %s::jsonb, %s)
                ON CONFLICT (run_id, event_key) DO NOTHING
                """,
                (
                    self.id_factory(),
                    request.owner_id,
                    action[0],
                    f"publish:{prepared.action_id}:published",
                    json.dumps(
                        {
                            "action_id": prepared.action_id,
                            "remote_comment_id": comment.remote_id,
                            "head_sha": request.head_sha,
                            "finding_ids": list(request.finding_ids),
                            "approval_ids": list(request.approval_ids),
                        }
                    ),
                    occurred_at,
                ),
            )

    def _record_failure(
        self,
        request: PublishRequest,
        action_id: str,
        failure_code: str,
    ) -> None:
        occurred_at = self.now().astimezone(UTC)
        with self.connection_factory() as connection, connection.transaction():
            action = connection.execute(
                """
                UPDATE external_actions AS action
                SET status = 'failed', updated_at = now()
                FROM runs AS run
                WHERE action.run_id = run.id
                  AND action.owner_id = run.owner_id
                  AND action.owner_id = %s
                  AND action.public_id = %s
                  AND run.public_id = %s
                RETURNING action.run_id
                """,
                (request.owner_id, action_id, request.run_id),
            ).fetchone()
            if action is None:
                raise RuntimeError("publish action disappeared before failure audit")
            connection.execute(
                """
                INSERT INTO run_events (
                    public_id, owner_id, run_id, event_key, event_type,
                    event_data, occurred_at
                ) VALUES (%s, %s, %s, %s, 'github.comment_publish_failed', %s::jsonb, %s)
                ON CONFLICT (run_id, event_key) DO NOTHING
                """,
                (
                    self.id_factory(),
                    request.owner_id,
                    action[0],
                    f"publish:{action_id}:failed:{failure_code}",
                    json.dumps(
                        {
                            "action_id": action_id,
                            "head_sha": request.head_sha,
                            "failure_code": failure_code,
                        }
                    ),
                    occurred_at,
                ),
            )


def _validate_request_shape(request: PublishRequest) -> None:
    if len(set(request.finding_ids)) != len(request.finding_ids):
        raise PublishBlockedError("finding IDs must be unique")
    if len(set(request.approval_ids)) != len(request.approval_ids):
        raise PublishBlockedError("approval IDs must be unique")
    if len(request.finding_ids) != len(request.approval_ids):
        raise PublishBlockedError("each finding needs one approval")
    expected_key = f"{request.run_id}:{request.head_sha}:publish"
    if request.idempotency_key != expected_key:
        raise PublishBlockedError("publish idempotency key is invalid")


def _comment_marker(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return f"<!-- pr-reliability:{digest} -->"


def _render_comment(findings: list[tuple[Any, ...]], marker: str) -> str:
    sections = ["## PR Reliability review"]
    for _, severity, claim, *_ in findings:
        sections.append(f"### {severity.title()} finding\n\n{claim}")
    sections.append(marker)
    body = "\n\n".join(sections)
    if len(body) > _MAX_COMMENT_CHARACTERS:
        raise PublishBlockedError("approved comment exceeds the publish limit")
    return body


def _blocked_application_error(error: PublishBlockedError) -> ApplicationError:
    return ApplicationError(str(error), type="PublishBlocked", non_retryable=True)
