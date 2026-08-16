"""Runnable FastAPI application for webhook intake."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from datetime import timedelta

import psycopg
from fastapi import FastAPI, Request
from opentelemetry import propagate, trace
from pr_reliability_observability import configure_telemetry, tracer
from psycopg import Connection
from temporalio.client import Client

from .db import apply_migrations
from .health import DatabaseHealthCheck, WorkflowHealthCheck, create_health_router
from .webhooks import GithubWebhookSettings, create_github_webhook_router


def create_app(
    settings: GithubWebhookSettings,
    connection_factory: Callable[[], Connection[object]],
    workflow_health_check: WorkflowHealthCheck | None = None,
    *,
    database_health_check: DatabaseHealthCheck | None = None,
    health_check_timeout_seconds: float = 2.0,
) -> FastAPI:
    """Create the API with explicit production or test dependencies."""

    app = FastAPI(title="PR Reliability API")
    app.include_router(
        create_health_router(
            database_health_check or _unconfigured_database_health,
            workflow_health_check or _unconfigured_workflow_health,
            timeout_seconds=health_check_timeout_seconds,
        )
    )
    app.include_router(create_github_webhook_router(settings, connection_factory))

    @app.middleware("http")
    async def trace_request(request: Request, call_next):
        parent = propagate.extract(dict(request.headers))
        with tracer().start_as_current_span(
            f"{request.method} {request.url.path}",
            context=parent,
            kind=trace.SpanKind.SERVER,
            attributes={"http.request.method": request.method, "url.path": request.url.path},
        ) as span:
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
            span_context = span.get_span_context()
            if span_context.is_valid:
                response.headers["X-Trace-Id"] = format(span_context.trace_id, "032x")
            return response

    return app


def create_app_from_environment() -> FastAPI:
    """Create the production API from required environment variables."""

    database_url = _required_environment("DATABASE_URL")
    health_check_timeout_seconds = _positive_float_environment("HEALTH_CHECK_TIMEOUT_SECONDS", 2.0)
    configure_telemetry("pr-reliability-api")
    settings = GithubWebhookSettings(
        owner_id=_required_environment("OWNER_ID"),
        installation_id=int(_required_environment("GITHUB_INSTALLATION_ID")),
        webhook_secret=_required_environment("GITHUB_WEBHOOK_SECRET").encode(),
    )
    with psycopg.connect(database_url) as connection:
        apply_migrations(connection)
    workflow_health_check = _temporal_health_check(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        os.environ.get("TEMPORAL_NAMESPACE", "default"),
        health_check_timeout_seconds,
    )
    return create_app(
        settings,
        lambda: psycopg.connect(
            database_url,
            connect_timeout=max(1, math.ceil(health_check_timeout_seconds)),
        ),
        workflow_health_check,
        database_health_check=_database_health_check(
            database_url,
            health_check_timeout_seconds,
        ),
        health_check_timeout_seconds=health_check_timeout_seconds,
    )


def _database_health_check(
    database_url: str,
    timeout_seconds: float,
    *,
    connect=psycopg.AsyncConnection.connect,
) -> DatabaseHealthCheck:
    connect_timeout_seconds = max(1, math.ceil(timeout_seconds))
    statement_timeout_ms = max(1, math.ceil(timeout_seconds * 1_000))

    async def check() -> None:
        connection = await connect(
            database_url,
            connect_timeout=connect_timeout_seconds,
            options=f"-c statement_timeout={statement_timeout_ms}",
        )
        try:
            await connection.execute("SELECT 1")
        finally:
            await connection.close()

    return check


def _temporal_health_check(
    address: str, namespace: str, timeout_seconds: float
) -> WorkflowHealthCheck:
    client: Client | None = None

    async def check() -> None:
        nonlocal client
        if client is None:
            client = await Client.connect(address, namespace=namespace, lazy=True)
        healthy = await client.service_client.check_health(
            timeout=timedelta(seconds=timeout_seconds)
        )
        if not healthy:
            raise RuntimeError("workflow service is unhealthy")

    return check


async def _unconfigured_workflow_health() -> None:
    raise RuntimeError("workflow health check is not configured")


async def _unconfigured_database_health() -> None:
    raise RuntimeError("database health check is not configured")


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def _positive_float_environment(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive number") from error
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value
