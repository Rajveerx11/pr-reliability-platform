"""Liveness and dependency-aware readiness checks."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from psycopg import Error as PsycopgError
from temporalio.service import RPCError

DependencyHealthCheck = Callable[[], Awaitable[None]]
DatabaseHealthCheck = DependencyHealthCheck
WorkflowHealthCheck = DependencyHealthCheck


def create_health_router(
    database_health_check: DatabaseHealthCheck,
    workflow_health_check: WorkflowHealthCheck,
    *,
    timeout_seconds: float = 2.0,
) -> APIRouter:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("health check timeout must be positive")
    router = APIRouter()
    probe_lock = asyncio.Lock()
    active_probe: asyncio.Task[dict[str, str]] | None = None

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @router.get("/health/ready")
    async def ready():
        nonlocal active_probe
        async with probe_lock:
            if active_probe is None or active_probe.done():
                active_probe = asyncio.create_task(
                    _check_dependencies(
                        database_health_check,
                        workflow_health_check,
                        timeout_seconds,
                    )
                )
            probe = active_probe
        dependencies = await asyncio.shield(probe)
        ready_status = all(value == "ready" for value in dependencies.values())
        body = {"status": "ready" if ready_status else "not_ready", "dependencies": dependencies}
        if not ready_status:
            return JSONResponse(status_code=503, content=body)
        return body

    return router


async def _check_dependencies(
    database_health_check: DatabaseHealthCheck,
    workflow_health_check: WorkflowHealthCheck,
    timeout_seconds: float,
) -> dict[str, str]:
    database, workflow = await asyncio.gather(
        _safe_check(database_health_check, timeout_seconds),
        _safe_check(workflow_health_check, timeout_seconds),
    )
    return {"database": database, "workflow": workflow}


async def _safe_check(check: DependencyHealthCheck, timeout_seconds: float) -> str:
    try:
        await asyncio.wait_for(check(), timeout=timeout_seconds)
    except (OSError, PsycopgError, RPCError, RuntimeError, TimeoutError):
        return "unavailable"
    return "ready"
