"""PostgreSQL outbox tests for production Temporal command dispatch."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from opentelemetry import trace
from pr_reliability_api.db import apply_migrations
from pr_reliability_contracts import StartRunCommand
from pr_reliability_workers.dispatch import (
    dispatch_next_command,
    dispatch_pending_commands,
    dispatch_start_run,
    workflow_id_for,
)
from psycopg import Connection

OWNER_ID = "01J00000000000000000000001"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
TASK_QUEUE = "review-workflow-tests"
TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


def public_id(sequence: int) -> str:
    return f"01J{sequence:023d}"


@pytest.fixture
def connection_factory() -> Iterator[Callable[[], Connection[object]]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        if os.environ.get("CI"):
            pytest.fail("CI must provide TEST_DATABASE_URL")
        pytest.skip("TEST_DATABASE_URL is required")

    schema = f"test_{uuid4().hex}"
    with psycopg.connect(database_url) as setup:
        setup.execute(f'CREATE SCHEMA "{schema}"')
        setup.execute(f'SET search_path TO "{schema}"')
        setup.commit()
        apply_migrations(setup)

    def create() -> Connection[object]:
        connection = psycopg.connect(database_url)
        connection.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    try:
        yield create
    finally:
        with psycopg.connect(database_url) as cleanup:
            cleanup.execute(f'DROP SCHEMA "{schema}" CASCADE')


class RecordingTemporalClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.trace_ids: list[int] = []
        self.failures_remaining = 0

    async def start_workflow(self, _workflow, request, **options):
        self.calls.append((request, options))
        self.trace_ids.append(trace.get_current_span().get_span_context().trace_id)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ConnectionError("Temporal unavailable")
        return SimpleNamespace(id=options["id"])


def test_dispatch_restores_persisted_traceparent() -> None:
    command = StartRunCommand(
        schema_version="1.1",
        public_id=public_id(4),
        owner_id=OWNER_ID,
        run_id=public_id(3),
        generation=1,
        repository_id=public_id(1),
        pull_request_id=public_id(2),
        pull_request_number=12,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        token_budget=100_000,
        cost_budget_usd_micros=1_000_000,
        traceparent=TRACEPARENT,
    )
    client = RecordingTemporalClient()

    asyncio.run(dispatch_start_run(client, command, task_queue=TASK_QUEUE))  # type: ignore[arg-type]

    assert client.trace_ids == [int("0123456789abcdef0123456789abcdef", 16)]


def seed_command(
    connection_factory: Callable[[], Connection[object]],
    *,
    command_pull_request_number: int = 12,
) -> StartRunCommand:
    command = StartRunCommand(
        schema_version="1.1",
        public_id=public_id(4),
        owner_id=OWNER_ID,
        run_id=public_id(3),
        generation=1,
        repository_id=public_id(1),
        pull_request_id=public_id(2),
        pull_request_number=command_pull_request_number,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        token_budget=100_000,
        cost_budget_usd_micros=1_000_000,
        traceparent=TRACEPARENT,
    )
    with connection_factory() as connection, connection.transaction():
        repository_id = connection.execute(
            """
            INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
            VALUES (%s, %s, 91, 'owner/repository') RETURNING id
            """,
            (command.repository_id, OWNER_ID),
        ).fetchone()[0]
        pull_request_id = connection.execute(
            """
            INSERT INTO pull_requests (
                public_id, owner_id, repository_id, github_number, base_sha, head_sha
            ) VALUES (%s, %s, %s, 12, %s, %s) RETURNING id
            """,
            (command.pull_request_id, OWNER_ID, repository_id, BASE_SHA, HEAD_SHA),
        ).fetchone()[0]
        run_id = connection.execute(
            """
            INSERT INTO runs (
                public_id, owner_id, pull_request_id, base_sha, head_sha,
                token_budget, cost_budget_usd_micros, generation
            ) VALUES (%s, %s, %s, %s, %s, 100000, 1000000, 1) RETURNING id
            """,
            (command.run_id, OWNER_ID, pull_request_id, BASE_SHA, HEAD_SHA),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO run_events (
                public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
            ) VALUES (%s, %s, %s, %s, 'run.command_created', %s::jsonb, now())
            """,
            (public_id(5), OWNER_ID, run_id, command.public_id, command.model_dump_json()),
        )
    return command


def test_pending_command_is_dispatched_once_and_receipted(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    command = seed_command(connection_factory)
    client = RecordingTemporalClient()

    first = asyncio.run(
        dispatch_next_command(
            connection_factory,
            client,
            task_queue=TASK_QUEUE,
            id_factory=lambda: public_id(6),
        )
    )
    second = asyncio.run(
        dispatch_next_command(
            connection_factory,
            client,
            task_queue=TASK_QUEUE,
            id_factory=lambda: public_id(7),
        )
    )

    assert first is True
    assert second is False
    assert len(client.calls) == 1
    request, options = client.calls[0]
    assert request.run_id == command.run_id
    assert request.generation == command.generation
    assert options["request_id"] == command.public_id
    assert options["id"] == workflow_id_for(command)
    assert options["rpc_timeout"].total_seconds() == 10
    assert client.trace_ids == [int("0123456789abcdef0123456789abcdef", 16)]
    with connection_factory() as connection:
        receipt = connection.execute(
            """
            SELECT event_data
            FROM run_events
            WHERE event_type = 'run.command_dispatched'
            """
        ).fetchone()[0]
    assert receipt == {
        "command_id": command.public_id,
        "status": "accepted",
        "workflow_id": workflow_id_for(command),
    }


def test_failed_temporal_send_leaves_command_pending(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_command(connection_factory)
    client = RecordingTemporalClient()
    client.failures_remaining = 1

    with pytest.raises(ConnectionError, match="Temporal unavailable"):
        asyncio.run(
            dispatch_next_command(
                connection_factory,
                client,
                task_queue=TASK_QUEUE,
                id_factory=lambda: public_id(6),
            )
        )

    with connection_factory() as connection:
        receipt_count = connection.execute(
            "SELECT count(*) FROM run_events WHERE event_type = 'run.command_dispatched'"
        ).fetchone()[0]
    assert receipt_count == 0

    assert asyncio.run(
        dispatch_next_command(
            connection_factory,
            client,
            task_queue=TASK_QUEUE,
            id_factory=lambda: public_id(6),
        )
    )


def test_concurrent_dispatchers_do_not_send_same_command(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_command(connection_factory)

    async def run() -> None:
        class BlockingTemporalClient(RecordingTemporalClient):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def start_workflow(self, workflow, request, **options):
                self.started.set()
                await self.release.wait()
                return await super().start_workflow(workflow, request, **options)

        client = BlockingTemporalClient()
        first = asyncio.create_task(
            dispatch_next_command(
                connection_factory,
                client,
                task_queue=TASK_QUEUE,
                id_factory=lambda: public_id(6),
            )
        )
        await client.started.wait()
        second = await dispatch_next_command(
            connection_factory,
            client,
            task_queue=TASK_QUEUE,
            id_factory=lambda: public_id(7),
        )
        client.release.set()

        assert second is False
        assert await first is True
        assert len(client.calls) == 1

    asyncio.run(run())


def test_dispatch_holds_run_lock_until_temporal_receipt_commits(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    command = seed_command(connection_factory)

    async def run() -> None:
        class BlockingTemporalClient(RecordingTemporalClient):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def start_workflow(self, workflow, request, **options):
                self.started.set()
                await self.release.wait()
                return await super().start_workflow(workflow, request, **options)

        client = BlockingTemporalClient()
        dispatch = asyncio.create_task(
            dispatch_next_command(
                connection_factory,
                client,
                task_queue=TASK_QUEUE,
                id_factory=lambda: public_id(6),
            )
        )
        await client.started.wait()

        def mark_terminal() -> None:
            with connection_factory() as connection, connection.transaction():
                connection.execute(
                    "UPDATE runs SET state = 'failed' WHERE public_id = %s",
                    (command.run_id,),
                )

        terminal_update = asyncio.create_task(asyncio.to_thread(mark_terminal))
        await asyncio.sleep(0.05)
        assert not terminal_update.done()

        client.release.set()
        assert await dispatch is True
        await terminal_update

        with connection_factory() as connection:
            state = connection.execute(
                "SELECT state FROM runs WHERE public_id = %s", (command.run_id,)
            ).fetchone()[0]
        assert state == "failed"

    asyncio.run(run())


def test_dispatch_timeout_releases_run_lock_and_leaves_command_pending(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    command = seed_command(connection_factory)

    async def run() -> None:
        class HangingTemporalClient(RecordingTemporalClient):
            async def start_workflow(self, _workflow, _request, **_options):
                await asyncio.Event().wait()

        with pytest.raises(TimeoutError):
            await dispatch_next_command(
                connection_factory,
                HangingTemporalClient(),
                task_queue=TASK_QUEUE,
                id_factory=lambda: public_id(6),
                dispatch_timeout_seconds=0.05,
            )

        def mark_terminal() -> None:
            with connection_factory() as connection, connection.transaction():
                connection.execute(
                    "UPDATE runs SET state = 'failed' WHERE public_id = %s",
                    (command.run_id,),
                )

        await asyncio.wait_for(asyncio.to_thread(mark_terminal), timeout=1)
        with connection_factory() as connection:
            state, receipt_count = connection.execute(
                """
                SELECT state, (
                    SELECT count(*) FROM run_events WHERE event_type = 'run.command_dispatched'
                )
                FROM runs WHERE public_id = %s
                """,
                (command.run_id,),
            ).fetchone()
        assert state == "failed"
        assert receipt_count == 0

    asyncio.run(run())


def test_continuous_dispatcher_recovers_from_transient_failure(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_command(connection_factory)

    async def run() -> None:
        stop = asyncio.Event()

        class RecoveringTemporalClient(RecordingTemporalClient):
            async def start_workflow(self, workflow, request, **options):
                result = await super().start_workflow(workflow, request, **options)
                stop.set()
                return result

        client = RecoveringTemporalClient()
        client.failures_remaining = 1
        await dispatch_pending_commands(
            connection_factory,
            client,
            task_queue=TASK_QUEUE,
            poll_interval_seconds=0.01,
            stop_event=stop,
        )

        assert len(client.calls) == 2

    asyncio.run(run())


def test_relational_identity_mismatch_is_rejected(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    seed_command(connection_factory, command_pull_request_number=13)
    client = RecordingTemporalClient()

    with pytest.raises(ValueError, match="does not match relational state"):
        asyncio.run(
            dispatch_next_command(
                connection_factory,
                client,
                task_queue=TASK_QUEUE,
                id_factory=lambda: public_id(6),
            )
        )

    assert client.calls == []


def test_superseded_generation_is_receipted_without_dispatch(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    command = seed_command(connection_factory)
    with connection_factory() as connection, connection.transaction():
        pull_request_id = connection.execute(
            "SELECT pull_request_id FROM runs WHERE public_id = %s",
            (command.run_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO runs (
                public_id, owner_id, pull_request_id, base_sha, head_sha,
                token_budget, cost_budget_usd_micros, generation
            ) VALUES (%s, %s, %s, %s, %s, 100000, 1000000, 2)
            """,
            (public_id(8), OWNER_ID, pull_request_id, BASE_SHA, "c" * 40),
        )
    client = RecordingTemporalClient()
    event_ids = iter((public_id(6), public_id(7)))

    assert asyncio.run(
        dispatch_next_command(
            connection_factory,
            client,
            task_queue=TASK_QUEUE,
            id_factory=lambda: next(event_ids),
        )
    )
    assert client.calls == []
    with connection_factory() as connection:
        run_state = connection.execute(
            "SELECT state FROM runs WHERE public_id = %s", (command.run_id,)
        ).fetchone()[0]
        events = connection.execute(
            """
            SELECT event_type, event_data
            FROM run_events
            WHERE event_type IN ('run.cancelled', 'run.command_dispatched')
            ORDER BY id
            """
        ).fetchall()
    assert run_state == "cancelled"
    assert events[0] == (
        "run.cancelled",
        {
            "outcome": "cancelled",
            "reason": "superseded before dispatch",
            "superseded_by_generation": 2,
        },
    )
    receipt = events[1][1]
    assert receipt == {
        "command_id": command.public_id,
        "reason": "superseded generation",
        "status": "skipped",
    }


def test_terminal_run_is_receipted_without_dispatch(
    connection_factory: Callable[[], Connection[object]],
) -> None:
    command = seed_command(connection_factory)
    with connection_factory() as connection, connection.transaction():
        connection.execute(
            "UPDATE runs SET state = 'failed' WHERE public_id = %s", (command.run_id,)
        )
    client = RecordingTemporalClient()

    assert asyncio.run(
        dispatch_next_command(
            connection_factory,
            client,
            task_queue=TASK_QUEUE,
            id_factory=lambda: public_id(6),
        )
    )
    assert client.calls == []
