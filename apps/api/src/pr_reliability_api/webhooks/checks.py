"""Authenticate GitHub Check Run reruns and create one new review generation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from fastapi import HTTPException, status
from pr_reliability_contracts import StartRunCommand
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from ..repositories.store import admitted_policy, lock_installation

_CHECK_RUN_NAME = "PR Reliability review"
IdFactory = Callable[[], str]


class CheckRerunSettings(Protocol):
    owner_id: str
    installation_id: int
    app_id: int | None


class _Installation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictInt = Field(gt=0)


class _Repository(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictInt = Field(gt=0)
    full_name: str = Field(min_length=1)


class _CheckRunApp(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictInt = Field(gt=0)


class _RequestedAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    identifier: str = Field(min_length=1, max_length=20)


class _CheckRun(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictInt = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    external_id: str = Field(min_length=1, max_length=255)
    app: _CheckRunApp


class CheckRunPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str = Field(min_length=1, max_length=64)
    requested_action: _RequestedAction | None = None
    installation: _Installation
    repository: _Repository
    check_run: _CheckRun

    @model_validator(mode="after")
    def require_supported_action(self) -> CheckRunPayload:
        if self.action == "requested_action" and (
            self.requested_action is None or self.requested_action.identifier != "rerun"
        ):
            raise ValueError("unsupported Check Run action")
        return self


def receive_check_rerun(
    connection: Connection[Any],
    settings: CheckRerunSettings,
    delivery_id: str,
    payload: CheckRunPayload,
    received_at: datetime,
    id_factory: IdFactory,
    traceparent: str | None,
) -> dict[str, object]:
    """Deduplicate an owned completed check rerun and enqueue its next generation."""

    if settings.app_id is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub App ID is not configured")
    if payload.check_run.app.id != settings.app_id or payload.check_run.name != _CHECK_RUN_NAME:
        return {"accepted": True, "duplicate": False, "command_id": None}
    if payload.action not in {"rerequested", "requested_action"}:
        return {"accepted": True, "duplicate": False, "command_id": None}
    expected_external_id = re.fullmatch(
        r"pr-reliability:([0-7][0-9A-HJKMNP-TV-Z]{25}):([0-9a-f]{40})",
        payload.check_run.external_id,
    )
    if expected_external_id is None or expected_external_id.group(2) != payload.check_run.head_sha:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid payload")

    lock_installation(connection, settings.owner_id, settings.installation_id)
    delivery = _insert_delivery(connection, settings, delivery_id, payload, received_at, id_factory)
    if delivery is None:
        return {"accepted": True, "duplicate": True, "command_id": None}

    row = _locked_rerun_target(connection, settings, payload, expected_external_id.group(1))
    command_public_id = None
    if row is not None:
        (
            current_generation,
            pull_request_id,
            pull_request_public_id,
            pull_request_number,
            base_sha,
            current_head_sha,
            pull_request_state,
            repository_public_id,
            base_branch,
            latest_generation,
        ) = row
        policy = admitted_policy(
            connection,
            settings.owner_id,
            settings.installation_id,
            payload.repository.id,
            base_branch,
        )
        if (
            policy is not None
            and pull_request_state == "open"
            and current_head_sha == payload.check_run.head_sha
            and current_generation == latest_generation
        ):
            command_public_id = _create_rerun(
                connection,
                settings,
                pull_request_id,
                pull_request_public_id,
                pull_request_number,
                repository_public_id,
                base_sha,
                payload.check_run.head_sha,
                base_branch,
                policy,
                id_factory,
                traceparent,
            )
    connection.execute(
        "UPDATE github_check_rerun_deliveries SET command_public_id = %s WHERE id = %s",
        (command_public_id, delivery),
    )
    return {"accepted": True, "duplicate": False, "command_id": command_public_id}


def _insert_delivery(
    connection: Connection[Any],
    settings: CheckRerunSettings,
    delivery_id: str,
    payload: CheckRunPayload,
    received_at: datetime,
    id_factory: IdFactory,
) -> int | None:
    row = connection.execute(
        """
        INSERT INTO github_check_rerun_deliveries (
            public_id, owner_id, delivery_id, installation_id, repository_github_id,
            check_run_remote_id, action, head_sha, received_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (owner_id, delivery_id) DO NOTHING
        RETURNING id
        """,
        (
            id_factory(),
            settings.owner_id,
            delivery_id,
            payload.installation.id,
            payload.repository.id,
            payload.check_run.id,
            payload.action,
            payload.check_run.head_sha,
            received_at,
        ),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _locked_rerun_target(
    connection: Connection[Any],
    settings: CheckRerunSettings,
    payload: CheckRunPayload,
    pull_request_public_id: str,
):
    return connection.execute(
        """
        SELECT current.generation, pull_request.id, pull_request.public_id,
               pull_request.github_number, pull_request.base_sha, pull_request.head_sha,
               pull_request.state, repository.public_id, current.base_branch,
               (SELECT max(latest.generation) FROM runs AS latest
                WHERE latest.pull_request_id = pull_request.id)
        FROM github_check_runs AS check_run
        JOIN runs AS current
          ON current.id = check_run.current_run_id AND current.owner_id = check_run.owner_id
        JOIN pull_requests AS pull_request
          ON pull_request.id = check_run.pull_request_id
         AND pull_request.owner_id = check_run.owner_id
        JOIN repositories AS repository
          ON repository.id = pull_request.repository_id
         AND repository.owner_id = check_run.owner_id
        WHERE check_run.owner_id = %s
          AND check_run.remote_id = %s
          AND check_run.external_id = %s
          AND check_run.head_sha = %s
          AND check_run.status = 'completed'
          AND repository.github_repository_id = %s
          AND pull_request.public_id = %s
        FOR UPDATE OF check_run, pull_request
        """,
        (
            settings.owner_id,
            payload.check_run.id,
            payload.check_run.external_id,
            payload.check_run.head_sha,
            payload.repository.id,
            pull_request_public_id,
        ),
    ).fetchone()


def _create_rerun(
    connection: Connection[Any],
    settings: CheckRerunSettings,
    pull_request_id: int,
    pull_request_public_id: str,
    pull_request_number: int,
    repository_public_id: str,
    base_sha: str,
    head_sha: str,
    base_branch: str,
    policy,
    id_factory: IdFactory,
    traceparent: str | None,
) -> str:
    run_public_id = id_factory()
    generation = connection.execute(
        "SELECT COALESCE(MAX(generation), 0) + 1 FROM runs WHERE pull_request_id = %s",
        (pull_request_id,),
    ).fetchone()[0]
    run_id = connection.execute(
        """
        INSERT INTO runs (
            public_id, owner_id, pull_request_id, base_sha, head_sha,
            token_budget, cost_budget_usd_micros, generation, base_branch, verification_profile
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            run_public_id,
            settings.owner_id,
            pull_request_id,
            base_sha,
            head_sha,
            policy.token_budget,
            policy.cost_budget_usd_micros,
            generation,
            base_branch,
            policy.verification_profile,
        ),
    ).fetchone()[0]
    command_public_id = id_factory()
    command = StartRunCommand(
        schema_version="1.1",
        public_id=command_public_id,
        owner_id=settings.owner_id,
        run_id=run_public_id,
        generation=generation,
        head_sha=head_sha,
        repository_id=repository_public_id,
        pull_request_id=pull_request_public_id,
        pull_request_number=pull_request_number,
        base_sha=base_sha,
        token_budget=policy.token_budget,
        cost_budget_usd_micros=policy.cost_budget_usd_micros,
        traceparent=traceparent,
    )
    connection.execute(
        """UPDATE repositories SET last_review_run_at = now()
           WHERE owner_id = %s AND public_id = %s""",
        (settings.owner_id, repository_public_id),
    )
    connection.execute(
        """
        INSERT INTO run_events (
            public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
        ) VALUES (%s, %s, %s, %s, 'run.command_created', %s::jsonb, now())
        """,
        (
            id_factory(),
            settings.owner_id,
            run_id,
            command_public_id,
            json.dumps(command.model_dump(mode="json")),
        ),
    )
    return command_public_id
