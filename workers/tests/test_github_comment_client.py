"""Contract tests for the production GitHub comment client."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pr_reliability_workers.activities import GitHubRestCommentClient

HEAD_SHA = "b" * 40
MARKER = "<!-- pr-reliability:" + "d" * 64 + " -->"
TOKEN = "installation-secret-token"
APP_AUTHOR_ID = 777


def response(request: httpx.Request, status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=request)


def test_comment_lookup_pages_and_ignores_foreign_marker() -> None:
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        page = int(request.url.params["page"])
        requested_pages.append(page)
        if page == 1:
            comments = [
                {"id": value, "body": MARKER if value == 1 else "other", "user": {"id": 999}}
                for value in range(1, 101)
            ]
            return response(request, 200, comments)
        assert page == 2
        return response(
            request,
            200,
            [{"id": 101, "body": f"approved\n\n{MARKER}", "user": {"id": APP_AUTHOR_ID}}],
        )

    client = GitHubRestCommentClient(
        TOKEN,
        APP_AUTHOR_ID,
        transport=httpx.MockTransport(handler),
    )

    comment = asyncio.run(client.find_comment("owner/repository", 17, MARKER))

    assert comment is not None
    assert comment.remote_id == "101"
    assert requested_pages == [1, 2]


def test_head_and_comment_creation_use_repository_scoped_paths() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return response(request, 200, {"head": {"sha": HEAD_SHA}})
        assert request.method == "POST"
        assert request.read() == b'{"body":"approved body"}'
        return response(request, 201, {"id": 2001})

    client = GitHubRestCommentClient(
        TOKEN,
        APP_AUTHOR_ID,
        transport=httpx.MockTransport(handler),
    )

    head = asyncio.run(client.current_head_sha("owner/repository", 17))
    comment = asyncio.run(client.create_comment("owner/repository", 17, "approved body"))

    assert head == HEAD_SHA
    assert comment.remote_id == "2001"
    assert [request.url.path for request in requests] == [
        "/repos/owner/repository/pulls/17",
        "/repos/owner/repository/issues/17/comments",
    ]


def test_github_failure_does_not_expose_token_or_response_body() -> None:
    secret_body = "provider-response-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=secret_body, request=request)

    client = GitHubRestCommentClient(
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
    client = GitHubRestCommentClient(TOKEN, APP_AUTHOR_ID)

    with pytest.raises(ValueError, match="repository"):
        asyncio.run(client.current_head_sha(repository, 17))
