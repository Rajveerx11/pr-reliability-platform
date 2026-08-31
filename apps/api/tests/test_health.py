"""Tests for liveness and dependency-aware readiness."""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pr_reliability_api.app import _database_health_check
from pr_reliability_api.health import create_health_router


def client(
    *,
    database_failing: bool = False,
    workflow_failing: bool = False,
    database_health_check=None,
    timeout_seconds: float = 2.0,
) -> TestClient:
    async def database_health() -> None:
        if database_failing:
            raise ConnectionError("database unavailable")

    async def workflow_health() -> None:
        if workflow_failing:
            raise ConnectionError("workflow unavailable")

    app = FastAPI()
    app.include_router(
        create_health_router(
            database_health_check or database_health,
            workflow_health,
            timeout_seconds=timeout_seconds,
        )
    )
    return TestClient(app)


def test_liveness_does_not_require_dependencies() -> None:
    response = client(database_failing=True, workflow_failing=True).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_readiness_reports_both_dependencies() -> None:
    response = client().get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"database": "ready", "workflow": "ready"},
    }


def test_readiness_fails_closed_without_leaking_errors() -> None:
    response = client(database_failing=True, workflow_failing=True).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependencies": {"database": "unavailable", "workflow": "unavailable"},
    }


def test_repeated_database_timeouts_cancel_and_close_connections() -> None:
    state = {"active": 0, "closed": 0, "started": 0}
    connect_options: list[dict[str, object]] = []

    class FrozenAsyncConnection:
        async def close(self) -> None:
            state["active"] -= 1
            state["closed"] += 1

        async def execute(self, statement: str) -> None:
            assert statement == "SELECT 1"
            state["started"] += 1
            await asyncio.Event().wait()

    async def connect(database_url: str, **options):
        assert database_url == "postgresql://database/health"
        connect_options.append(options)
        state["active"] += 1
        return FrozenAsyncConnection()

    database_health = _database_health_check(
        "postgresql://database/health",
        0.02,
        connect=connect,
    )
    test_client = client(database_health_check=database_health, timeout_seconds=0.02)
    with test_client:
        started_at = time.perf_counter()
        responses = [test_client.get("/health/ready") for _ in range(5)]
        elapsed = time.perf_counter() - started_at

    assert elapsed < 1
    assert all(response.status_code == 503 for response in responses)
    assert state == {"active": 0, "closed": 5, "started": 5}
    assert connect_options == [{"connect_timeout": 1, "options": "-c statement_timeout=20"}] * 5


def test_concurrent_readiness_requests_share_one_dependency_probe() -> None:
    calls = {"database": 0, "workflow": 0}

    async def run() -> None:
        release = asyncio.Event()

        async def database_health() -> None:
            calls["database"] += 1
            await release.wait()

        async def workflow_health() -> None:
            calls["workflow"] += 1
            await release.wait()

        app = FastAPI()
        app.include_router(create_health_router(database_health, workflow_health))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            requests = [asyncio.create_task(client.get("/health/ready")) for _ in range(20)]
            for _ in range(100):
                if calls == {"database": 1, "workflow": 1}:
                    break
                await asyncio.sleep(0.001)
            assert calls == {"database": 1, "workflow": 1}
            release.set()
            responses = await asyncio.gather(*requests)

        assert all(response.status_code == 200 for response in responses)

    asyncio.run(run())

    assert calls == {"database": 1, "workflow": 1}
