"""Runnable FastAPI application for webhook intake."""

from __future__ import annotations

import os
from collections.abc import Callable

import psycopg
from fastapi import FastAPI
from psycopg import Connection

from .approvals import ApprovalInboxSettings, create_approval_inbox_router
from .db import apply_migrations
from .webhooks import GithubWebhookSettings, create_github_webhook_router


def create_app(
    settings: GithubWebhookSettings,
    connection_factory: Callable[[], Connection[object]],
    approval_settings: ApprovalInboxSettings | None = None,
) -> FastAPI:
    """Create the API with explicit production or test dependencies."""

    app = FastAPI(title="PR Reliability API")
    app.include_router(create_github_webhook_router(settings, connection_factory))
    if approval_settings is not None:
        app.include_router(create_approval_inbox_router(approval_settings, connection_factory))
    return app


def create_app_from_environment() -> FastAPI:
    """Create the production API from required environment variables."""

    database_url = _required_environment("DATABASE_URL")
    settings = GithubWebhookSettings(
        owner_id=_required_environment("OWNER_ID"),
        installation_id=int(_required_environment("GITHUB_INSTALLATION_ID")),
        webhook_secret=_required_environment("GITHUB_WEBHOOK_SECRET").encode(),
    )
    approval_settings = ApprovalInboxSettings(
        owner_id=settings.owner_id,
        actor_id=_required_environment("APPROVAL_ACTOR_ID"),
        reviewer_token=_required_environment("APPROVAL_REVIEWER_TOKEN"),
    )
    with psycopg.connect(database_url) as connection:
        apply_migrations(connection)
    return create_app(settings, lambda: psycopg.connect(database_url), approval_settings)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value
