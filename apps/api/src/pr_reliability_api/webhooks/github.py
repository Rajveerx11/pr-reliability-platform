"""Verify and atomically deduplicate supported GitHub pull request webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pr_reliability_contracts import PullRequestAction, StartRunCommand
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

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
    base: _Branch
    head: _Branch


class _PullRequestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: PullRequestAction
    installation: _Installation
    repository: _Repository
    pull_request: _PullRequest


def create_github_webhook_router(
    settings: GithubWebhookSettings,
    connection_factory: ConnectionFactory,
    *,
    id_factory: IdFactory = lambda: _new_ulid(),
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
                    head_sha, received_at
                )
                VALUES (%s, %s, %s, 'pull_request', %s, %s, %s, %s, %s, %s)
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
                    received_at,
                ),
            ).fetchone()
            if delivery is None:
                return {"accepted": True, "duplicate": True, "command_id": None}

            repository_id, repository_public_id = _upsert_repository(
                connection, settings.owner_id, payload, id_factory
            )
            pull_request_id, pull_request_public_id = _upsert_pull_request(
                connection, settings.owner_id, repository_id, payload, id_factory
            )
            command_public_id = None
            if payload.action is not PullRequestAction.CLOSED:
                command_public_id = _create_run(
                    connection,
                    settings,
                    payload,
                    pull_request_id,
                    repository_public_id,
                    pull_request_public_id,
                    id_factory,
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
        SET full_name = EXCLUDED.full_name, updated_at = now()
        RETURNING id, public_id
        """,
        (id_factory(), owner_id, payload.repository.id, payload.repository.full_name),
    ).fetchone()


def _upsert_pull_request(
    connection: Connection[Any],
    owner_id: str,
    repository_id: int,
    payload: _PullRequestPayload,
    id_factory: IdFactory,
) -> tuple[int, str]:
    pr_state = "closed" if payload.action is PullRequestAction.CLOSED else "open"
    return connection.execute(
        """
        INSERT INTO pull_requests (
            public_id, owner_id, repository_id, github_number,
            base_sha, head_sha, state
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (repository_id, github_number) DO UPDATE
        SET base_sha = EXCLUDED.base_sha,
            head_sha = EXCLUDED.head_sha,
            state = EXCLUDED.state,
            updated_at = now()
        RETURNING id, public_id
        """,
        (
            id_factory(),
            owner_id,
            repository_id,
            payload.pull_request.number,
            payload.pull_request.base.sha,
            payload.pull_request.head.sha,
            pr_state,
        ),
    ).fetchone()


def _create_run(
    connection: Connection[Any],
    settings: GithubWebhookSettings,
    payload: _PullRequestPayload,
    pull_request_id: int,
    repository_public_id: str,
    pull_request_public_id: str,
    id_factory: IdFactory,
) -> str | None:
    run_public_id = id_factory()
    run = connection.execute(
        """
        INSERT INTO runs (
            public_id, owner_id, pull_request_id, base_sha, head_sha,
            token_budget, cost_budget_usd_micros
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (pull_request_id, head_sha) DO NOTHING
        RETURNING id, public_id
        """,
        (
            run_public_id,
            settings.owner_id,
            pull_request_id,
            payload.pull_request.base.sha,
            payload.pull_request.head.sha,
            settings.token_budget,
            settings.cost_budget_usd_micros,
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
        head_sha=payload.pull_request.head.sha,
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


def _new_ulid() -> str:
    value = (time.time_ns() // 1_000_000 << 80) | secrets.randbits(80)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    encoded = ""
    for _ in range(26):
        encoded = alphabet[value & 31] + encoded
        value >>= 5
    return encoded
