"""Runner health and graceful drain around the existing Temporal worker."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import timedelta
from uuid import uuid4

import psycopg
from pr_reliability_api.operations.store import (
    OperationsStore,
    RunnerRegistration,
    RunnerSessionReplaced,
)
from temporalio.service import RPCError
from temporalio.worker import ActivityInboundInterceptor, Interceptor

from .pending_activities import observe_pending_activities

_LOG = logging.getLogger(__name__)


class RunnerMonitor(Interceptor):
    def __init__(self, store: OperationsStore, registration: RunnerRegistration):
        self.store = store
        self.registration = registration
        self.active = 0
        self.draining = False
        self.stop = asyncio.Event()

    def intercept_activity(self, next):
        return _ObservedActivity(next, self)

    async def pulse(self, healthy: bool = True) -> None:
        state = "draining" if self.draining else "busy" if self.active else "online"
        if not healthy:
            state = "offline"
        requested = await asyncio.to_thread(
            self.store.heartbeat, self.registration, self.active, state
        )
        if requested:
            self.drain()

    def drain(self):
        self.draining = True
        self.stop.set()

    async def watch(self, client, interval: float = 10):
        while True:
            try:
                healthy = await client.service_client.check_health(timeout=timedelta(seconds=3))
            except (RPCError, ConnectionError, TimeoutError):
                healthy = False
                _LOG.warning("runner heartbeat dependency unavailable")
            try:
                await self.pulse(healthy)
                if healthy and self.registration.workload == "workflow":
                    await observe_pending_activities(client, self.store, self.registration)
            except RunnerSessionReplaced:
                self.drain()
                return
            except (psycopg.Error, ConnectionError, TimeoutError):
                # Keep Temporal work durable during a dependency outage. No exception details.
                _LOG.warning("runner heartbeat dependency unavailable")
            await asyncio.sleep(interval)


class _ObservedActivity(ActivityInboundInterceptor):
    def __init__(self, next, monitor):
        super().__init__(next)
        self.monitor = monitor

    async def execute_activity(self, input):
        monitor = self.monitor
        monitor.active += 1
        try:
            # Fail before the provider side effect if persistence is unavailable. Temporal retries.
            if input.args:
                await asyncio.to_thread(
                    monitor.store.observe_start, monitor.registration, input.args[0]
                )
            return await self.next.execute_activity(input)
        finally:
            monitor.active -= 1


def monitor_from_environment(queue: str, workload: str) -> RunnerMonitor:
    def required(name):
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"{name} is required for runner operations")
        return value

    database_url = required("DATABASE_URL")
    registration = RunnerRegistration(
        owner_id=required("OWNER_ID"),
        runner_id=required("RUNNER_ID"),
        session_id=uuid4(),
        queue=queue,
        version=required("RUNNER_VERSION"),
        workload=workload,
        capacity=int(os.environ.get("RUNNER_CAPACITY", "4")) if workload == "review" else 1,
    )
    return RunnerMonitor(
        OperationsStore(
            lambda: psycopg.connect(
                database_url, connect_timeout=5, options="-c statement_timeout=5000"
            )
        ),
        registration,
    )


async def run_monitored(worker, client, monitor: RunnerMonitor):
    """SIGTERM or durable drain stops polling, then lets the SDK finish in-flight work.

    Worker graceful_shutdown_timeout bounds activity grace; interrupted attempts are retried
    by Temporal. Do not cancel workflows or remove commands, leases, or run state here.
    """
    await asyncio.to_thread(monitor.store.register, monitor.registration)
    # A drain left by a crashed process must be observed before any new Temporal polling.
    await monitor.pulse()
    if monitor.stop.is_set():
        await asyncio.to_thread(monitor.store.retire, monitor.registration)
        return
    loop = asyncio.get_running_loop()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, monitor.drain)
            previous[sig] = None
        except NotImplementedError:
            previous[sig] = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(monitor.drain))
    watch = asyncio.create_task(monitor.watch(client))
    polling = asyncio.create_task(worker.run())
    stopping = asyncio.create_task(monitor.stop.wait())
    try:
        await asyncio.wait((polling, stopping), return_when=asyncio.FIRST_COMPLETED)
        monitor.draining = True
        try:
            await monitor.pulse()
        except (psycopg.Error, RunnerSessionReplaced, ConnectionError, TimeoutError):
            _LOG.warning("runner drain heartbeat unavailable")
        await worker.shutdown()
        await polling
    finally:
        if not polling.done():
            monitor.draining = True
            await worker.shutdown()
            await asyncio.gather(polling, return_exceptions=True)
        # Keep draining heartbeats during SDK shutdown, then make capacity unavailable.
        watch.cancel()
        stopping.cancel()
        await asyncio.gather(watch, stopping, return_exceptions=True)
        try:
            await asyncio.to_thread(monitor.store.retire, monitor.registration)
        except (psycopg.Error, ConnectionError, TimeoutError):
            _LOG.warning("runner offline heartbeat unavailable")
        for sig, handler in previous.items():
            if handler is None:
                loop.remove_signal_handler(sig)
            else:
                signal.signal(sig, handler)
