"""Reliably dispatch persisted run commands to Temporal."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
from opentelemetry import context as otel_context
from pr_reliability_contracts import ApprovalCommand, ApprovalDecision, StartRunCommand
from pr_reliability_observability import configure_telemetry, context_from_traceparent
from psycopg import Connection
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError

from .workflows import (
    ApprovalSignal,
    PullRequestReviewWorkflow,
    ReviewWorkflowInput,
    SupersedeSignal,
)

ConnectionFactory = Callable[[], Connection[Any]]
_TERMINAL_RUN_STATES = frozenset({"published", "rejected", "failed", "cancelled"})
_TRANSIENT_ERRORS = (psycopg.Error, RPCError, ConnectionError, TimeoutError)
_TEMPORAL_RPC_TIMEOUT = timedelta(seconds=10)
_LOGGER = logging.getLogger(__name__)


def workflow_id_for(command: StartRunCommand) -> str:
    """Keep one active workflow execution per owned pull request."""

    return _workflow_id(command.owner_id, command.pull_request_id)


def _workflow_id(owner_id: str, pull_request_id: str) -> str:
    return f"pr-review:{owner_id}:{pull_request_id}"


async def dispatch_start_run(
    client: Client,
    command: StartRunCommand,
    *,
    task_queue: str,
) -> WorkflowHandle[ReviewWorkflowInput, object]:
    """Signal the active run or atomically start the first run for this pull request."""

    request = ReviewWorkflowInput(
        owner_id=command.owner_id,
        run_id=command.run_id,
        generation=command.generation,
        repository_id=command.repository_id,
        pull_request_id=command.pull_request_id,
        pull_request_number=command.pull_request_number,
        base_sha=command.base_sha,
        head_sha=command.head_sha,
        token_budget=command.token_budget,
        cost_budget_usd_micros=command.cost_budget_usd_micros,
        traceparent=command.traceparent,
    )
    parent = context_from_traceparent(command.traceparent)
    token = otel_context.attach(parent) if parent is not None else None
    try:
        return await client.start_workflow(
            PullRequestReviewWorkflow.run,
            request,
            id=workflow_id_for(command),
            task_queue=task_queue,
            execution_timeout=timedelta(days=30),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            start_signal="supersede",
            start_signal_args=[SupersedeSignal(request)],
            request_id=command.public_id,
            rpc_timeout=_TEMPORAL_RPC_TIMEOUT,
        )
    finally:
        if token is not None:
            otel_context.detach(token)


async def dispatch_next_command(
    connection_factory: ConnectionFactory,
    client: Client,
    *,
    task_queue: str,
    id_factory: Callable[[], str] = lambda: _new_ulid(),
    dispatch_timeout_seconds: float = 11.0,
) -> bool:
    """Dispatch one pending outbox command and append its durable receipt.

    Relational generation and terminal state are permanent reconciliation guards. Stable
    Temporal request and activity keys keep a retry safe if the process exits after acceptance
    but before the receipt commits.
    """

    if dispatch_timeout_seconds <= 0:
        raise ValueError("dispatch_timeout_seconds must be positive")
    with connection_factory() as connection, connection.transaction():
        row = connection.execute(
            """
            SELECT command.run_id, command.owner_id, command.event_key, command.event_data,
                   run.public_id, run.state, run.generation, run.base_sha, run.head_sha,
                   run.token_budget, run.cost_budget_usd_micros,
                   pull_request.public_id, pull_request.github_number, repository.public_id,
                   (
                       SELECT max(latest.generation)
                       FROM runs AS latest
                       WHERE latest.pull_request_id = run.pull_request_id
                   ) AS latest_generation
            FROM run_events AS command
            JOIN runs AS run
              ON run.id = command.run_id
             AND run.owner_id = command.owner_id
            JOIN pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
             AND pull_request.owner_id = run.owner_id
            JOIN repositories AS repository
              ON repository.id = pull_request.repository_id
             AND repository.owner_id = pull_request.owner_id
            WHERE command.event_type = 'run.command_created'
              AND NOT EXISTS (
                  SELECT 1
                  FROM run_events AS receipt
                  WHERE receipt.run_id = command.run_id
                    AND receipt.event_key = command.event_key || ':dispatched'
              )
            ORDER BY command.id
            FOR UPDATE OF command, run SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return False

        (
            internal_run_id,
            owner_id,
            event_key,
            event_data,
            run_public_id,
            run_state,
            generation,
            base_sha,
            head_sha,
            token_budget,
            cost_budget_usd_micros,
            pull_request_public_id,
            pull_request_number,
            repository_public_id,
            latest_generation,
        ) = row
        command = StartRunCommand.model_validate(event_data)
        persisted_identity = (
            event_key,
            owner_id,
            run_public_id,
            generation,
            repository_public_id,
            pull_request_public_id,
            pull_request_number,
            base_sha,
            head_sha,
            token_budget,
            cost_budget_usd_micros,
        )
        command_identity = (
            command.public_id,
            command.owner_id,
            command.run_id,
            command.generation,
            command.repository_id,
            command.pull_request_id,
            command.pull_request_number,
            command.base_sha,
            command.head_sha,
            command.token_budget,
            command.cost_budget_usd_micros,
        )
        if command_identity != persisted_identity:
            raise ValueError("persisted start-run command does not match relational state")

        if generation < latest_generation:
            _cancel_superseded_queued_run(
                connection,
                command,
                internal_run_id,
                latest_generation,
                id_factory,
            )
            _insert_receipt(
                connection,
                command,
                internal_run_id,
                id_factory,
                status="skipped",
                reason="superseded generation",
            )
            return True
        if run_state in _TERMINAL_RUN_STATES:
            _insert_receipt(
                connection,
                command,
                internal_run_id,
                id_factory,
                status="skipped",
                reason=f"run already {run_state}",
            )
            return True

        async with asyncio.timeout(dispatch_timeout_seconds):
            handle = await dispatch_start_run(client, command, task_queue=task_queue)
        _insert_receipt(
            connection,
            command,
            internal_run_id,
            id_factory,
            status="accepted",
            workflow_id=handle.id,
        )
        return True


async def dispatch_next_approval(
    connection_factory: ConnectionFactory,
    client: Client,
    *,
    id_factory: Callable[[], str] = lambda: _new_ulid(),
    dispatch_timeout_seconds: float = 11.0,
) -> bool:
    """Signal one durable human decision to its waiting workflow exactly once."""

    if dispatch_timeout_seconds <= 0:
        raise ValueError("dispatch_timeout_seconds must be positive")
    with connection_factory() as connection, connection.transaction():
        row = connection.execute(
            """
            SELECT command.run_id, command.owner_id, command.event_key, command.event_data,
                   run.public_id, run.state, run.head_sha,
                   pull_request.public_id, pull_request.head_sha,
                   approval.public_id, approval.actor_id, approval.decision,
                   approval.reason, approval.decided_at, finding.public_id
            FROM run_events AS command
            JOIN runs AS run
              ON run.id = command.run_id
             AND run.owner_id = command.owner_id
            JOIN pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
             AND pull_request.owner_id = run.owner_id
            JOIN approvals AS approval
              ON approval.run_id = run.id
             AND approval.owner_id = run.owner_id
            JOIN findings AS finding
              ON finding.id = approval.finding_id
             AND finding.run_id = run.id
             AND finding.owner_id = run.owner_id
            WHERE command.event_type = 'approval.signal_created'
              AND approval.public_id = command.event_data->>'public_id'
              AND NOT EXISTS (
                  SELECT 1
                  FROM run_events AS receipt
                  WHERE receipt.run_id = command.run_id
                    AND receipt.event_key = command.event_key || ':dispatched'
              )
            ORDER BY command.id
            FOR UPDATE OF command, run, pull_request, approval SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return False

        (
            internal_run_id,
            owner_id,
            event_key,
            event_data,
            run_public_id,
            run_state,
            run_head_sha,
            pull_request_public_id,
            current_head_sha,
            approval_public_id,
            actor_id,
            decision,
            reason,
            decided_at,
            finding_public_id,
        ) = row
        command = ApprovalCommand.model_validate(event_data)
        persisted_identity = (
            event_key,
            approval_public_id,
            owner_id,
            run_public_id,
            run_head_sha,
            finding_public_id,
            actor_id,
            decision,
            reason,
            decided_at,
        )
        command_identity = (
            f"approval:{command.public_id}:signal",
            command.public_id,
            command.owner_id,
            command.run_id,
            command.head_sha,
            command.finding_id,
            command.actor_id,
            command.decision.value,
            command.reason,
            command.decided_at,
        )
        if command_identity != persisted_identity:
            raise ValueError("persisted approval command does not match relational state")

        if run_state != "awaiting_approval" or current_head_sha != run_head_sha:
            _insert_approval_receipt(
                connection,
                command,
                internal_run_id,
                event_key,
                id_factory,
                status="skipped",
                reason="run no longer awaits this commit",
            )
            return True

        approved = command.decision is ApprovalDecision.APPROVED
        signal = ApprovalSignal(
            run_id=command.run_id,
            head_sha=command.head_sha,
            approved=approved,
            finding_ids=(command.finding_id,) if approved else (),
            approval_ids=(command.public_id,) if approved else (),
            comment_body_ref=f"approval:{command.public_id}" if approved else None,
        )
        handle = client.get_workflow_handle(_workflow_id(command.owner_id, pull_request_public_id))
        async with asyncio.timeout(dispatch_timeout_seconds):
            await handle.signal(
                PullRequestReviewWorkflow.approve,
                signal,
                rpc_timeout=_TEMPORAL_RPC_TIMEOUT,
            )
        _insert_approval_receipt(
            connection,
            command,
            internal_run_id,
            event_key,
            id_factory,
            status="accepted",
        )
        return True


async def dispatch_pending_commands(
    connection_factory: ConnectionFactory,
    client: Client,
    *,
    task_queue: str,
    poll_interval_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Continuously drain the PostgreSQL command outbox and retry transient failures."""

    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    while stop_event is None or not stop_event.is_set():
        dispatched = False
        for dispatch_kind in ("approval", "start"):
            try:
                if dispatch_kind == "approval":
                    result = await dispatch_next_approval(connection_factory, client)
                else:
                    result = await dispatch_next_command(
                        connection_factory,
                        client,
                        task_queue=task_queue,
                    )
                dispatched = result or dispatched
            except _TRANSIENT_ERRORS:
                _LOGGER.warning(
                    "transient %s dispatch failure; retrying",
                    dispatch_kind,
                    exc_info=True,
                )
        if not dispatched:
            await _wait_or_stop(poll_interval_seconds, stop_event)


async def run_dispatcher_from_environment() -> None:
    """Run the production command dispatcher from environment settings."""

    from pr_reliability_api.db import apply_migrations

    database_url = _required_environment("DATABASE_URL")
    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    temporal_namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "pr-review")

    with psycopg.connect(database_url) as connection:
        apply_migrations(connection)
    tracing = configure_telemetry("pr-reliability-command-dispatcher")
    client = await Client.connect(
        temporal_address,
        namespace=temporal_namespace,
        interceptors=[tracing],
    )
    await dispatch_pending_commands(
        lambda: psycopg.connect(database_url),
        client,
        task_queue=task_queue,
    )


def main() -> None:
    """Start the production command dispatcher."""

    asyncio.run(run_dispatcher_from_environment())


def _insert_receipt(
    connection: Connection[Any],
    command: StartRunCommand,
    internal_run_id: int,
    id_factory: Callable[[], str],
    *,
    status: str,
    workflow_id: str | None = None,
    reason: str | None = None,
) -> None:
    event_data = {"command_id": command.public_id, "status": status}
    if workflow_id is not None:
        event_data["workflow_id"] = workflow_id
    if reason is not None:
        event_data["reason"] = reason
    connection.execute(
        """
        INSERT INTO run_events (
            public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
        )
        VALUES (%s, %s, %s, %s, 'run.command_dispatched', %s::jsonb, now())
        ON CONFLICT (run_id, event_key) DO NOTHING
        """,
        (
            id_factory(),
            command.owner_id,
            internal_run_id,
            f"{command.public_id}:dispatched",
            json.dumps(event_data),
        ),
    )


def _insert_approval_receipt(
    connection: Connection[Any],
    command: ApprovalCommand,
    internal_run_id: int,
    event_key: str,
    id_factory: Callable[[], str],
    *,
    status: str,
    reason: str | None = None,
) -> None:
    event_data = {"approval_id": command.public_id, "status": status}
    if reason is not None:
        event_data["reason"] = reason
    connection.execute(
        """
        INSERT INTO run_events (
            public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
        )
        VALUES (%s, %s, %s, %s, 'approval.signal_dispatched', %s::jsonb, now())
        ON CONFLICT (run_id, event_key) DO NOTHING
        """,
        (
            id_factory(),
            command.owner_id,
            internal_run_id,
            f"{event_key}:dispatched",
            json.dumps(event_data),
        ),
    )


def _cancel_superseded_queued_run(
    connection: Connection[Any],
    command: StartRunCommand,
    internal_run_id: int,
    latest_generation: int,
    id_factory: Callable[[], str],
) -> None:
    cancelled = connection.execute(
        """
        UPDATE runs
        SET state = 'cancelled', updated_at = now()
        WHERE id = %s AND owner_id = %s AND state = 'queued'
        RETURNING id
        """,
        (internal_run_id, command.owner_id),
    ).fetchone()
    if cancelled is None:
        return
    connection.execute(
        """
        INSERT INTO run_events (
            public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
        )
        VALUES (%s, %s, %s, %s, 'run.cancelled', %s::jsonb, now())
        ON CONFLICT (run_id, event_key) DO NOTHING
        """,
        (
            id_factory(),
            command.owner_id,
            internal_run_id,
            f"{command.run_id}:{command.head_sha}:terminal:cancelled",
            json.dumps(
                {
                    "outcome": "cancelled",
                    "reason": "superseded before dispatch",
                    "superseded_by_generation": latest_generation,
                }
            ),
        ),
    )


async def _wait_or_stop(seconds: float, stop_event: asyncio.Event | None) -> None:
    if stop_event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def _new_ulid() -> str:
    value = (time.time_ns() // 1_000_000 << 80) | secrets.randbits(80)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    encoded = ""
    for _ in range(26):
        encoded = alphabet[value & 31] + encoded
        value >>= 5
    return encoded
