"""Verify and atomically deduplicate supported GitHub pull request webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pr_reliability_contracts import PullRequestAction, StartRunCommand
from psycopg import Connection
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from ..identifiers import new_ulid

ConnectionFactory = Callable[[], Connection[Any]]
IdFactory = Callable[[], str]


@dataclass(frozen=True, slots=True)
class GithubWebhookSettings:
    """Secrets and defaults needed by webhook intake."""

    owner_id: str
    installation_id: int
    webhook_secret: bytes
    token_budget: int = 100_000
    cost_budget_usd_micros: int = 1_000_000

    def __post_init__(self) -> None:
        if not self.webhook_secret:
            raise ValueError("webhook_secret must not be empty")
        if self.installation_id < 1:
            raise ValueError("installation_id must be positive")
        if self.token_budget < 1 or self.cost_budget_usd_micros < 0:
            raise ValueError("review budgets are invalid")


class _Installation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictInt = Field(gt=0)


class _Repository(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictInt = Field(gt=0)
    full_name: str = Field(min_length=1)


class _Branch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class _PullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    number: StrictInt = Field(gt=0)
    updated_at: AwareDatetime
    base: _Branch
    head: _Branch


class _PullRequestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: PullRequestAction
    before: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    after: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    installation: _Installation
    repository: _Repository
    pull_request: _PullRequest

    @model_validator(mode="after")
    def validate_synchronize_chain(self) -> _PullRequestPayload:
        if self.action is PullRequestAction.SYNCHRONIZE and (
            self.before is None or self.after != self.pull_request.head.sha
        ):
            raise ValueError("synchronize payload requires matching before and after SHAs")
        return self


def create_github_webhook_router(
    settings: GithubWebhookSettings,
    connection_factory: ConnectionFactory,
    *,
    id_factory: IdFactory = new_ulid,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Build a router with explicit, injectable persistence dependencies."""

    router = APIRouter()

    @router.post("/webhooks/github")
    async def receive_github_webhook(
        request: Request,
        x_hub_signature_256: str = Header(alias="X-Hub-Signature-256"),
        x_github_delivery: str = Header(alias="X-GitHub-Delivery", min_length=1, max_length=128),
        x_github_event: str = Header(alias="X-GitHub-Event"),
    ) -> dict[str, object]:
        raw_body = await request.body()
        if not _valid_signature(raw_body, x_hub_signature_256, settings.webhook_secret):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
        if x_github_event != "pull_request":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unsupported event")

        try:
            payload = _PullRequestPayload.model_validate(json.loads(raw_body))
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid payload") from None
        if payload.installation.id != settings.installation_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "installation is not authorized")

        received_at = now().astimezone(UTC)
        with connection_factory() as connection, connection.transaction():
            delivery = connection.execute(
                """
                INSERT INTO github_webhook_deliveries (
                    public_id, owner_id, delivery_id, event_type, action,
                    installation_id, repository_github_id, pull_request_number,
                    head_sha, before_sha, after_sha, pull_request_updated_at, received_at
                )
                VALUES (%s, %s, %s, 'pull_request', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (owner_id, delivery_id) DO NOTHING
                RETURNING id
                """,
                (
                    id_factory(),
                    settings.owner_id,
                    x_github_delivery,
                    payload.action.value,
                    payload.installation.id,
                    payload.repository.id,
                    payload.pull_request.number,
                    payload.pull_request.head.sha,
                    payload.before,
                    payload.after,
                    payload.pull_request.updated_at.astimezone(UTC),
                    received_at,
                ),
            ).fetchone()
            if delivery is None:
                return {"accepted": True, "duplicate": True, "command_id": None}

            repository_id, repository_public_id = _upsert_repository(
                connection, settings.owner_id, payload, id_factory
            )
            resolved_head_sha = _resolve_head_sha(connection, settings.owner_id, payload)
            pull_request_id, pull_request_public_id, current_delivery = _upsert_pull_request(
                connection,
                settings.owner_id,
                repository_id,
                payload,
                resolved_head_sha,
                received_at,
                x_github_delivery,
                id_factory,
            )
            if current_delivery:
                _update_repository_name(connection, repository_id, payload.repository.full_name)
            command_public_id = None
            if current_delivery and payload.action is not PullRequestAction.CLOSED:
                command_public_id = _create_run(
                    connection,
                    settings,
                    payload,
                    resolved_head_sha,
                    pull_request_id,
                    repository_public_id,
                    pull_request_public_id,
                    id_factory,
                    force_new=payload.action is PullRequestAction.REOPENED,
                )
            connection.execute(
                "UPDATE github_webhook_deliveries SET command_public_id = %s WHERE id = %s",
                (command_public_id, delivery[0]),
            )

        return {
            "accepted": True,
            "duplicate": False,
            "command_id": command_public_id,
        }

    return router


def _valid_signature(body: bytes, supplied: str, secret: bytes) -> bool:
    if re.fullmatch(r"sha256=[0-9a-f]{64}", supplied) is None:
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


def _upsert_repository(
    connection: Connection[Any],
    owner_id: str,
    payload: _PullRequestPayload,
    id_factory: IdFactory,
) -> tuple[int, str]:
    return connection.execute(
        """
        INSERT INTO repositories (
            public_id, owner_id, github_repository_id, full_name
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT (owner_id, github_repository_id) DO UPDATE
        SET full_name = repositories.full_name
        RETURNING id, public_id
        """,
        (id_factory(), owner_id, payload.repository.id, payload.repository.full_name),
    ).fetchone()


def _update_repository_name(
    connection: Connection[Any], repository_id: int, full_name: str
) -> None:
    connection.execute(
        "UPDATE repositories SET full_name = %s, updated_at = now() WHERE id = %s",
        (full_name, repository_id),
    )


def _upsert_pull_request(
    connection: Connection[Any],
    owner_id: str,
    repository_id: int,
    payload: _PullRequestPayload,
    resolved_head_sha: str,
    received_at: datetime,
    delivery_id: str,
    id_factory: IdFactory,
) -> tuple[int, str, bool]:
    pr_state = "closed" if payload.action is PullRequestAction.CLOSED else "open"
    updated = connection.execute(
        """
        INSERT INTO pull_requests (
            public_id, owner_id, repository_id, github_number,
            base_sha, head_sha, state, github_updated_at,
            github_delivery_received_at, github_delivery_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (repository_id, github_number) DO UPDATE
        SET base_sha = EXCLUDED.base_sha,
            head_sha = EXCLUDED.head_sha,
            state = EXCLUDED.state,
            github_updated_at = EXCLUDED.github_updated_at,
            github_delivery_received_at = EXCLUDED.github_delivery_received_at,
            github_delivery_id = EXCLUDED.github_delivery_id,
            updated_at = now()
        WHERE pull_requests.github_updated_at IS NULL
           OR pull_requests.github_updated_at < EXCLUDED.github_updated_at
           OR (
               pull_requests.github_updated_at = EXCLUDED.github_updated_at
               AND pull_requests.state = 'closed'
               AND EXCLUDED.state = 'open'
           )
           OR (
               %s
               AND pull_requests.github_updated_at = EXCLUDED.github_updated_at
               AND pull_requests.state = 'open'
               AND EXCLUDED.state = 'open'
               AND pull_requests.head_sha <> EXCLUDED.head_sha
           )
        RETURNING id, public_id, true
        """,
        (
            id_factory(),
            owner_id,
            repository_id,
            payload.pull_request.number,
            payload.pull_request.base.sha,
            resolved_head_sha,
            pr_state,
            payload.pull_request.updated_at.astimezone(UTC),
            received_at,
            delivery_id,
            payload.action is PullRequestAction.SYNCHRONIZE,
        ),
    ).fetchone()
    if updated is not None:
        return updated
    existing = connection.execute(
        """
        SELECT id, public_id, false
        FROM pull_requests
        WHERE repository_id = %s AND github_number = %s
        """,
        (repository_id, payload.pull_request.number),
    ).fetchone()
    if existing is None:
        raise RuntimeError("pull request disappeared during webhook processing")
    return existing


def _resolve_head_sha(
    connection: Connection[Any], owner_id: str, payload: _PullRequestPayload
) -> str:
    if payload.action is not PullRequestAction.SYNCHRONIZE:
        return payload.pull_request.head.sha
    return connection.execute(
        """
        WITH RECURSIVE chain (sha, depth, path) AS (
            VALUES (%s::varchar(40), 0, ARRAY[%s::varchar(40)])
            UNION ALL
            SELECT delivery.after_sha, chain.depth + 1,
                   (chain.path || delivery.after_sha)::varchar(40)[]
            FROM chain
            JOIN github_webhook_deliveries AS delivery
              ON delivery.owner_id = %s
             AND delivery.repository_github_id = %s
             AND delivery.pull_request_number = %s
             AND delivery.action = 'synchronize'
             AND delivery.pull_request_updated_at = %s
             AND delivery.before_sha = chain.sha
            WHERE delivery.after_sha IS NOT NULL
              AND NOT delivery.after_sha = ANY(chain.path)
              AND chain.depth < 100
        )
        SELECT sha FROM chain ORDER BY depth DESC, sha DESC LIMIT 1
        """,
        (
            payload.after,
            payload.after,
            owner_id,
            payload.repository.id,
            payload.pull_request.number,
            payload.pull_request.updated_at.astimezone(UTC),
        ),
    ).fetchone()[0]


def _create_run(
    connection: Connection[Any],
    settings: GithubWebhookSettings,
    payload: _PullRequestPayload,
    resolved_head_sha: str,
    pull_request_id: int,
    repository_public_id: str,
    pull_request_public_id: str,
    id_factory: IdFactory,
    *,
    force_new: bool,
) -> str | None:
    run_public_id = id_factory()
    generation = connection.execute(
        "SELECT COALESCE(MAX(generation), 0) + 1 FROM runs WHERE pull_request_id = %s",
        (pull_request_id,),
    ).fetchone()[0]
    if not force_new:
        existing = connection.execute(
            "SELECT 1 FROM runs WHERE pull_request_id = %s AND head_sha = %s",
            (pull_request_id, resolved_head_sha),
        ).fetchone()
        if existing is not None:
            return None
    run = connection.execute(
        """
        INSERT INTO runs (
            public_id, owner_id, pull_request_id, base_sha, head_sha,
            token_budget, cost_budget_usd_micros, generation
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (pull_request_id, head_sha, generation) DO NOTHING
        RETURNING id, public_id
        """,
        (
            run_public_id,
            settings.owner_id,
            pull_request_id,
            payload.pull_request.base.sha,
            resolved_head_sha,
            settings.token_budget,
            settings.cost_budget_usd_micros,
            generation,
        ),
    ).fetchone()
    if run is None:
        return None

    command_public_id = id_factory()
    command = StartRunCommand(
        schema_version="1",
        public_id=command_public_id,
        owner_id=settings.owner_id,
        run_id=run[1],
        generation=generation,
        head_sha=resolved_head_sha,
        repository_id=repository_public_id,
        pull_request_id=pull_request_public_id,
        pull_request_number=payload.pull_request.number,
        base_sha=payload.pull_request.base.sha,
        token_budget=settings.token_budget,
        cost_budget_usd_micros=settings.cost_budget_usd_micros,
    )
    connection.execute(
        """
        INSERT INTO run_events (
            public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
        )
        VALUES (%s, %s, %s, %s, 'run.command_created', %s::jsonb, now())
        """,
        (
            id_factory(),
            settings.owner_id,
            run[0],
            command_public_id,
            command.model_dump_json(),
        ),
    )
    return command_public_id
