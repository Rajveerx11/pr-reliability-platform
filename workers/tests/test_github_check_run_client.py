"""Contract tests for the repository-scoped GitHub Check Run client."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pr_reliability_workers.activities import GitHubRestCheckRunClient

TOKEN = "check-run-installation-token"
APP_ID = 123
HEAD_SHA = "b" * 40
EXTERNAL_ID = f"pr-reliability:01J00000000000000000000005:{HEAD_SHA}"


def response(request: httpx.Request, status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=request)


def identity(remote_id: int) -> dict[str, object]:
    return {
        "id": remote_id,
        "head_sha": HEAD_SHA,
        "external_id": EXTERNAL_ID,
        "app": {"id": APP_ID},
    }


def test_recovers_owned_check_then_updates_without_mutating_commit() -> None:
    requests: list[httpx.Request] = []
    payload = {
        "name": "PR Reliability review",
        "head_sha": HEAD_SHA,
        "external_id": EXTERNAL_ID,
        "status": "in_progress",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.method == "GET":
            assert request.url.path == f"/repos/owner/repository/commits/{HEAD_SHA}/check-runs"
            assert request.url.params["check_name"] == "PR Reliability review"
            return response(
                request,
                200,
                {
                    "check_runs": [
                        {**identity(10), "external_id": "another-check"},
                        {**identity(11), "app": {"id": APP_ID + 1}},
                        identity(12),
                    ]
                },
            )
        assert request.method == "PATCH"
        assert request.url.path == "/repos/owner/repository/check-runs/12"
        sent = json.loads(request.content)
        assert sent["external_id"] == EXTERNAL_ID
        assert "head_sha" not in sent
        return response(request, 200, identity(12))

    client = GitHubRestCheckRunClient(
        TOKEN,
        APP_ID,
        transport=httpx.MockTransport(handler),
    )

    recovered = asyncio.run(client.find_check_run("owner/repository", HEAD_SHA, EXTERNAL_ID))
    assert recovered is not None and recovered.remote_id == 12
    updated = asyncio.run(client.update_check_run("owner/repository", 12, payload))

    assert updated == recovered
    assert [request.method for request in requests] == ["GET", "PATCH"]


def test_creates_commit_bound_check() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/owner/repository/check-runs"
        assert json.loads(request.content) == {
            "name": "PR Reliability review",
            "head_sha": HEAD_SHA,
            "external_id": EXTERNAL_ID,
            "status": "queued",
        }
        return response(request, 201, identity(22))

    client = GitHubRestCheckRunClient(
        TOKEN,
        APP_ID,
        transport=httpx.MockTransport(handler),
    )
    created = asyncio.run(
        client.create_check_run(
            "owner/repository",
            {
                "name": "PR Reliability review",
                "head_sha": HEAD_SHA,
                "external_id": EXTERNAL_ID,
                "status": "queued",
            },
        )
    )

    assert created.remote_id == 22
    assert created.head_sha == HEAD_SHA


def test_duplicate_recovery_matches_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(request, 200, {"check_runs": [identity(31), identity(32)]})

    client = GitHubRestCheckRunClient(
        TOKEN,
        APP_ID,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="duplicate"):
        asyncio.run(client.find_check_run("owner/repository", HEAD_SHA, EXTERNAL_ID))


def test_provider_failure_is_sanitized() -> None:
    secret = "private-provider-response"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=secret, request=request)

    client = GitHubRestCheckRunClient(
        TOKEN,
        APP_ID,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(client.find_check_run("owner/repository", HEAD_SHA, EXTERNAL_ID))

    assert str(raised.value) == "GitHub request failed"
    assert secret not in str(raised.value)
    assert TOKEN not in str(raised.value)
