"""Runner dependency failure, SDK shutdown, activity cleanup and restart fencing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import psycopg
import pytest
from pr_reliability_api.operations.store import RunnerRegistration, RunnerSessionReplaced
from pr_reliability_workers.operations import RunnerMonitor, run_monitored


def monitor():
    store = MagicMock()
    store.heartbeat.return_value = False
    return RunnerMonitor(
        store,
        RunnerRegistration(
            "01J00000000000000000000001", "review-1", uuid4(), "pr-review", "0.1.0", "review", 4
        ),
    )


def test_busy_slots_return_to_zero_after_failure_and_cancellation():
    async def scenario():
        runner = monitor()
        next = SimpleNamespace(
            execute_activity=AsyncMock(side_effect=RuntimeError("provider failed"))
        )
        interceptor = runner.intercept_activity(next)
        request = SimpleNamespace(owner_id=runner.registration.owner_id)
        with pytest.raises(RuntimeError):
            await interceptor.execute_activity(SimpleNamespace(args=[request]))
        assert runner.active == 0
        runner.store.observe_start.assert_called_once_with(runner.registration, request)
        next.execute_activity.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await interceptor.execute_activity(SimpleNamespace(args=[request]))
        assert runner.active == 0

    asyncio.run(scenario())


def test_database_loss_blocks_provider_then_retry_recovers():
    async def scenario():
        runner = monitor()
        runner.store.observe_start.side_effect = psycopg.OperationalError("test dependency failure")
        next = SimpleNamespace(execute_activity=AsyncMock(return_value="ok"))
        interceptor = runner.intercept_activity(next)
        input = SimpleNamespace(args=[SimpleNamespace(owner_id=runner.registration.owner_id)])
        with pytest.raises(psycopg.OperationalError):
            await interceptor.execute_activity(input)
        next.execute_activity.assert_not_called()
        assert runner.active == 0
        runner.store.observe_start.side_effect = None
        assert await interceptor.execute_activity(input) == "ok"

    asyncio.run(scenario())


def test_heartbeat_states_and_durable_drain():
    async def scenario():
        runner = monitor()
        await runner.pulse()
        assert runner.store.heartbeat.call_args.args[2] == "online"
        runner.active = 2
        await runner.pulse()
        assert runner.store.heartbeat.call_args.args[2] == "busy"
        await runner.pulse(healthy=False)
        assert runner.store.heartbeat.call_args.args[2] == "offline"
        runner.store.heartbeat.return_value = True
        await runner.pulse()
        assert runner.draining and runner.stop.is_set()
        await runner.pulse()
        assert runner.store.heartbeat.call_args.args[2] == "draining"

    asyncio.run(scenario())


def test_watch_survives_dependency_loss_and_drains_replaced_session(caplog):
    async def scenario():
        runner = monitor()
        checks = AsyncMock(side_effect=[ConnectionError("private-secret"), True, True])
        runner.store.heartbeat.side_effect = [False, False, RunnerSessionReplaced()]
        client = SimpleNamespace(service_client=SimpleNamespace(check_health=checks))
        await asyncio.wait_for(runner.watch(client, interval=0), 3)
        assert runner.draining
        assert checks.call_count == 3
        assert runner.store.heartbeat.call_args_list[0].args[2] == "offline"

    asyncio.run(scenario())
    assert "private-secret" not in caplog.text
    assert "dependency unavailable" in caplog.text


def test_drain_waits_for_sdk_shutdown_before_retiring_runner():
    async def scenario():
        runner = monitor()
        runner.store.heartbeat.side_effect = [False, True, True]
        finished = asyncio.Event()
        shutdown_called = asyncio.Event()

        async def poll():
            await finished.wait()

        async def shutdown():
            shutdown_called.set()
            runner.store.retire.assert_not_called()
            assert runner.draining
            assert runner.active == 1
            runner.active = 0
            finished.set()

        worker = SimpleNamespace(run=poll, shutdown=shutdown)
        client = SimpleNamespace(
            service_client=SimpleNamespace(check_health=AsyncMock(return_value=True))
        )
        runner.active = 1
        await asyncio.wait_for(run_monitored(worker, client, runner), 3)
        assert shutdown_called.is_set()
        runner.store.retire.assert_called_once_with(runner.registration)
        # No workflow cancellation or database run-state mutation belongs to the monitor.
        assert not hasattr(worker, "cancel_workflow")

    asyncio.run(scenario())


def test_task_cancellation_shuts_down_worker_before_retiring():
    async def scenario():
        runner = monitor()
        running = asyncio.Event()
        stopped = asyncio.Event()

        async def poll():
            running.set()
            await stopped.wait()

        async def shutdown():
            runner.store.retire.assert_not_called()
            stopped.set()

        worker = SimpleNamespace(run=poll, shutdown=shutdown)
        client = SimpleNamespace(
            service_client=SimpleNamespace(check_health=AsyncMock(return_value=True))
        )
        task = asyncio.create_task(run_monitored(worker, client, runner))
        await running.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        runner.store.retire.assert_called_once()

    asyncio.run(scenario())


def test_pending_drain_on_restart_never_starts_polling():
    async def scenario():
        runner = monitor()
        runner.store.heartbeat.return_value = True
        worker = SimpleNamespace(run=AsyncMock(), shutdown=AsyncMock())
        client = SimpleNamespace(
            service_client=SimpleNamespace(check_health=AsyncMock(return_value=True))
        )
        await run_monitored(worker, client, runner)
        worker.run.assert_not_called()
        worker.shutdown.assert_not_called()
        runner.store.retire.assert_called_once_with(runner.registration)
        assert runner.draining

    asyncio.run(scenario())


def test_database_loss_at_startup_never_starts_polling():
    async def scenario():
        runner = monitor()
        runner.store.heartbeat.side_effect = psycopg.OperationalError("test outage")
        worker = SimpleNamespace(run=AsyncMock(), shutdown=AsyncMock())
        with pytest.raises(psycopg.OperationalError):
            await run_monitored(worker, SimpleNamespace(), runner)
        worker.run.assert_not_called()
        runner.store.retire.assert_not_called()

    asyncio.run(scenario())
