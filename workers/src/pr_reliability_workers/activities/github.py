"""Repository-scoped GitHub REST client for approved comment publishing."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from .publish import GitHubComment

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_COMMENTS_PER_PAGE = 100


class GitHubRestCommentClient:
    """Use one installation token and verified App author for GitHub writes."""

    __slots__ = (
        "_api_url",
        "_authenticated_author_id",
        "_timeout_seconds",
        "_token",
        "_transport",
    )

    def __init__(
        self,
        token: str,
        authenticated_author_id: int,
        *,
        api_url: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("GitHub token is required")
        if authenticated_author_id < 1:
            raise ValueError("authenticated GitHub author ID must be positive")
        if timeout_seconds <= 0:
            raise ValueError("GitHub timeout must be positive")
        parsed_url = httpx.URL(api_url)
        if parsed_url.scheme != "https" or not parsed_url.host:
            raise ValueError("GitHub API URL must use HTTPS")
        self._token = token
        self._authenticated_author_id = authenticated_author_id
        self._api_url = str(parsed_url).rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def current_head_sha(self, repository: str, pull_request_number: int) -> str:
        path = _pull_request_path(repository, pull_request_number)
        async with self._new_http_client() as client:
            response = await client.get(path)
            _raise_for_status(response)
            payload = _json_object(response, "pull request")
        head = payload.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
            raise RuntimeError("GitHub returned an invalid pull request head")
        return sha

    async def find_comment(
        self,
        repository: str,
        pull_request_number: int,
        marker: str,
    ) -> GitHubComment | None:
        if not marker.startswith("<!-- pr-reliability:") or len(marker) > 96:
            raise ValueError("invalid GitHub retry marker")
        path = _comments_path(repository, pull_request_number)
        page = 1
        async with self._new_http_client() as client:
            while True:
                response = await client.get(
                    path,
                    params={"per_page": _COMMENTS_PER_PAGE, "page": page},
                )
                _raise_for_status(response)
                comments = _json_list(response, "comments")
                for value in comments:
                    if not isinstance(value, dict):
                        continue
                    user = value.get("user")
                    author_id = user.get("id") if isinstance(user, dict) else None
                    body = value.get("body")
                    if (
                        author_id == self._authenticated_author_id
                        and isinstance(body, str)
                        and marker in body
                    ):
                        return _comment_identity(value)
                if len(comments) < _COMMENTS_PER_PAGE:
                    return None
                page += 1

    async def create_comment(
        self,
        repository: str,
        pull_request_number: int,
        body: str,
    ) -> GitHubComment:
        path = _comments_path(repository, pull_request_number)
        async with self._new_http_client() as client:
            response = await client.post(path, json={"body": body})
            _raise_for_status(response)
            payload = _json_object(response, "comment")
        return _comment_identity(payload)

    def _new_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "pr-reliability-platform",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=self._timeout_seconds,
            transport=self._transport,
        )


def _pull_request_path(repository: str, pull_request_number: int) -> str:
    return f"repos/{_encoded_repository(repository)}/pulls/{_positive_number(pull_request_number)}"


def _comments_path(repository: str, pull_request_number: int) -> str:
    return f"repos/{_encoded_repository(repository)}/issues/{_positive_number(pull_request_number)}/comments"


def _encoded_repository(repository: str) -> str:
    if _REPOSITORY.fullmatch(repository) is None:
        raise ValueError("invalid GitHub repository name")
    owner, name = repository.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise ValueError("invalid GitHub repository name")
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _positive_number(value: int) -> int:
    if value < 1:
        raise ValueError("pull request number must be positive")
    return value


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_error:
        raise RuntimeError("GitHub request failed") from None


def _json_object(response: httpx.Response, subject: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(f"GitHub returned invalid {subject} JSON") from None
    if not isinstance(payload, dict):
        raise TypeError(f"GitHub returned an invalid {subject} response")
    return payload


def _json_list(response: httpx.Response, subject: str) -> list[Any]:
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(f"GitHub returned invalid {subject} JSON") from None
    if not isinstance(payload, list):
        raise TypeError(f"GitHub returned an invalid {subject} response")
    return payload


def _comment_identity(payload: dict[str, Any]) -> GitHubComment:
    remote_id = payload.get("id")
    if not isinstance(remote_id, (str, int)) or isinstance(remote_id, bool):
        raise TypeError("GitHub returned an invalid comment identity")
    return GitHubComment(str(remote_id))
