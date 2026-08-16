"""Contract tests for the production GitHub review client."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pr_reliability_workers.activities import (
    GitHubRestReviewClient,
    GitHubReviewPayloadMismatch,
)

HEAD_SHA = "b" * 40
NEXT_HEAD_SHA = "c" * 40
MARKER = "<!-- pr-reliability:" + "d" * 64 + " -->"
OTHER_MARKER = "<!-- pr-reliability:" + "e" * 64 + " -->"
EXPECTED_BODY = f"approved\n\n{MARKER}"
TOKEN = "installation-secret-token"
APP_AUTHOR_ID = 777


def response(request: httpx.Request, status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=request)


def test_review_lookup_pages_and_requires_author_body_and_commit() -> None:
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        page = int(request.url.params["page"])
        requested_pages.append(page)
        if page == 1:
            reviews = [
                {"id": value, "body": "other", "user": {"id": APP_AUTHOR_ID}, "commit_id": HEAD_SHA}
                for value in range(1, 101)
            ]
            reviews[0] = {
                "id": 1,
                "body": EXPECTED_BODY,
                "user": {"id": 999},
                "commit_id": HEAD_SHA,
            }
            reviews[1] = {
                "id": 2,
                "body": f"edited\n\n{MARKER}",
                "user": {"id": APP_AUTHOR_ID},
                "commit_id": HEAD_SHA,
            }
            reviews[2] = {
                "id": 3,
                "body": EXPECTED_BODY,
                "user": {"id": APP_AUTHOR_ID},
                "commit_id": NEXT_HEAD_SHA,
            }
            return response(request, 200, reviews)
        assert page == 2
        return response(
            request,
            200,
            [
                {
                    "id": 101,
                    "body": EXPECTED_BODY,
                    "user": {"id": APP_AUTHOR_ID},
                    "commit_id": HEAD_SHA,
                }
            ],
        )

    client = GitHubRestReviewClient(
        TOKEN,
        APP_AUTHOR_ID,
        transport=httpx.MockTransport(handler),
    )

    review = asyncio.run(
        client.find_review("owner/repository", 17, HEAD_SHA, MARKER, EXPECTED_BODY)
    )

    assert review is not None
    assert review.remote_id == "101"
    assert review.commit_sha == HEAD_SHA
    assert requested_pages == [1, 2]


def test_cross_marker_inside_approved_claim_is_not_a_recovery_match() -> None:
    injected_body = f"## Review A\n\nClaim quotes {MARKER}\n\n{OTHER_MARKER}"

    def handler(request: httpx.Request) -> httpx.Response:
        return response(
            request,
            200,
            [
                {
                    "id": 201,
                    "body": injected_body,
                    "user": {"id": APP_AUTHOR_ID},
                    "commit_id": HEAD_SHA,
                }
            ],
        )

    client = GitHubRestReviewClient(
        TOKEN,
        APP_AUTHOR_ID,
        transport=httpx.MockTransport(handler),
    )

    review = asyncio.run(
        client.find_review("owner/repository", 17, HEAD_SHA, MARKER, EXPECTED_BODY)
    )

    assert review is None


def test_owned_terminal_marker_with_wrong_body_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(
            request,
            200,
            [
                {
                    "id": 202,
                    "body": f"edited\n\n{MARKER}",
                    "user": {"id": APP_AUTHOR_ID},
                    "commit_id": HEAD_SHA,
                }
            ],
        )

    client = GitHubRestReviewClient(
        TOKEN,
        APP_AUTHOR_ID,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(GitHubReviewPayloadMismatch):
        asyncio.run(client.find_review("owner/repository", 17, HEAD_SHA, MARKER, EXPECTED_BODY))


def test_head_and_review_creation_use_commit_bound_repository_paths() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return response(request, 200, {"head": {"sha": HEAD_SHA}})
        assert request.method == "POST"
        assert request.read() == (
            b'{"body":"approved body","commit_id":"' + HEAD_SHA.encode() + b'","event":"COMMENT"}'
        )
        return response(request, 201, {"id": 2001, "commit_id": HEAD_SHA})

    client = GitHubRestReviewClient(
        TOKEN,
        APP_AUTHOR_ID,
        transport=httpx.MockTransport(handler),
    )

    head = asyncio.run(client.current_head_sha("owner/repository", 17))
    review = asyncio.run(client.create_review("owner/repository", 17, HEAD_SHA, "approved body"))

    assert head == HEAD_SHA
    assert review.remote_id == "2001"
    assert review.commit_sha == HEAD_SHA
    assert [request.url.path for request in requests] == [
        "/repos/owner/repository/pulls/17",
        "/repos/owner/repository/pulls/17/reviews",
    ]


def test_review_remains_bound_to_approved_commit_when_head_advances_after_precheck() -> None:
    remote_head = HEAD_SHA

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal remote_head
        if request.method == "GET":
            checked_head = remote_head
            remote_head = NEXT_HEAD_SHA
            return response(request, 200, {"head": {"sha": checked_head}})
        assert remote_head == NEXT_HEAD_SHA
        request_payload = json.loads(request.content)
        assert request_payload == {
            "body": EXPECTED_BODY,
            "commit_id": HEAD_SHA,
            "event": "COMMENT",
        }
        return response(request, 201, {"id": 2002, "commit_id": request_payload["commit_id"]})

    client = GitHubRestReviewClient(
        TOKEN,
        APP_AUTHOR_ID,
        transport=httpx.MockTransport(handler),
    )

    checked_head = asyncio.run(client.current_head_sha("owner/repository", 17))
    review = asyncio.run(client.create_review("owner/repository", 17, checked_head, EXPECTED_BODY))

    assert remote_head == NEXT_HEAD_SHA
    assert review.commit_sha == HEAD_SHA


def test_github_failure_does_not_expose_token_or_response_body() -> None:
    secret_body = "provider-response-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=secret_body, request=request)

    client = GitHubRestReviewClient(
        TOKEN,
        APP_AUTHOR_ID,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(client.current_head_sha("owner/repository", 17))

    failure = str(raised.value)
    assert failure == "GitHub request failed"
    assert TOKEN not in failure
    assert secret_body not in failure


@pytest.mark.parametrize("repository", ["owner", "../repository", "owner/repo/name"])
def test_repository_path_is_rejected(repository: str) -> None:
    client = GitHubRestReviewClient(TOKEN, APP_AUTHOR_ID)

    with pytest.raises(ValueError, match="repository"):
        asyncio.run(client.current_head_sha(repository, 17))
