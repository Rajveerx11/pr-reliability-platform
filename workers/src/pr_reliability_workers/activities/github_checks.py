"""Repository-scoped GitHub REST client for Check Runs."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ..providers.github_app import (
    CHECK_RUN_PERMISSIONS,
    GitHubAppInstallationTokenProvider,
)

CHECK_RUN_NAME = "PR Reliability review"
_GITHUB_API_URL = "https://api.github.com"
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class GitHubCheckRun:
    remote_id: int
    head_sha: str
    external_id: str


class GitHubCheckRunClient(Protocol):
    async def find_check_run(
        self, repository: str, head_sha: str, external_id: str
    ) -> GitHubCheckRun | None: ...

    async def create_check_run(
        self, repository: str, payload: dict[str, object]
    ) -> GitHubCheckRun: ...

    async def update_check_run(
        self, repository: str, remote_id: int, payload: dict[str, object]
    ) -> GitHubCheckRun: ...


class GitHubRestCheckRunClient:
    """Use a Checks-only installation token for one repository at a time."""

    def __init__(
        self,
        token: str | GitHubAppInstallationTokenProvider,
        app_id: int,
        *,
        repository_id_resolver: Callable[[str], int] | None = None,
        api_url: str = _GITHUB_API_URL,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if isinstance(token, GitHubAppInstallationTokenProvider):
            if repository_id_resolver is None:
                raise ValueError("GitHub repository ID resolver is required")
            self._token: str | None = None
            self._token_provider = token
        else:
            if not isinstance(token, str) or not token.strip():
                raise ValueError("GitHub token is required")
            if repository_id_resolver is not None:
                raise ValueError("GitHub repository ID resolver requires a token provider")
            self._token = token
            self._token_provider = None
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id < 1:
            raise ValueError("GitHub App ID must be positive")
        if timeout_seconds <= 0:
            raise ValueError("GitHub timeout must be positive")
        if str(httpx.URL(api_url)).rstrip("/") != _GITHUB_API_URL:
            raise ValueError("GitHub API URL must be https://api.github.com")
        self._app_id = app_id
        self._repository_id_resolver = repository_id_resolver
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    @property
    def uses_installation_token_provider(self) -> bool:
        return self._token_provider is not None

    async def find_check_run(
        self, repository: str, head_sha: str, external_id: str
    ) -> GitHubCheckRun | None:
        _require_sha(head_sha)
        path = f"repos/{_encoded_repository(repository)}/commits/{head_sha}/check-runs"
        matches: list[GitHubCheckRun] = []
        page = 1
        async with await self._new_http_client(repository) as client:
            while True:
                response = await client.get(
                    path,
                    params={
                        "check_name": CHECK_RUN_NAME,
                        "filter": "all",
                        "per_page": 100,
                        "page": page,
                    },
                )
                _raise_for_status(response)
                payload = _json_object(response, "check runs")
                values = payload.get("check_runs")
                if not isinstance(values, list):
                    raise TypeError("GitHub returned an invalid check runs response")
                for value in values:
                    if not isinstance(value, dict) or value.get("external_id") != external_id:
                        continue
                    app = value.get("app")
                    if not isinstance(app, dict) or app.get("id") != self._app_id:
                        continue
                    matches.append(_check_identity(value))
                if len(values) < 100:
                    break
                if page == 100:
                    raise RuntimeError("GitHub Check Run recovery exceeded its page limit")
                page += 1
        if len(matches) > 1:
            raise RuntimeError("GitHub returned duplicate PR Reliability checks")
        return matches[0] if matches else None

    async def create_check_run(self, repository: str, payload: dict[str, object]) -> GitHubCheckRun:
        path = f"repos/{_encoded_repository(repository)}/check-runs"
        async with await self._new_http_client(repository) as client:
            response = await client.post(path, json=payload)
            _raise_for_status(response)
            raw = _json_object(response, "check run")
            _require_owned(raw, self._app_id)
            result = _check_identity(raw)
        _require_matching_identity(result, payload)
        return result

    async def update_check_run(
        self, repository: str, remote_id: int, payload: dict[str, object]
    ) -> GitHubCheckRun:
        if isinstance(remote_id, bool) or not isinstance(remote_id, int) or remote_id < 1:
            raise ValueError("GitHub Check Run ID must be positive")
        path = f"repos/{_encoded_repository(repository)}/check-runs/{remote_id}"
        async with await self._new_http_client(repository) as client:
            response = await client.patch(
                path,
                json={key: value for key, value in payload.items() if key != "head_sha"},
            )
            _raise_for_status(response)
            raw = _json_object(response, "check run")
            _require_owned(raw, self._app_id)
            result = _check_identity(raw)
        if result.remote_id != remote_id:
            raise RuntimeError("GitHub updated an unexpected check run")
        _require_matching_identity(result, payload)
        return result

    async def _new_http_client(self, repository: str) -> httpx.AsyncClient:
        token = await self._token_for(repository)
        return httpx.AsyncClient(
            base_url=_GITHUB_API_URL + "/",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "pr-reliability-platform",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=self._timeout_seconds,
            transport=self._transport,
        )

    async def _token_for(self, repository: str) -> str:
        if self._token_provider is None:
            assert self._token is not None
            return self._token
        assert self._repository_id_resolver is not None
        try:
            repository_id = await asyncio.to_thread(self._repository_id_resolver, repository)
            if (
                isinstance(repository_id, bool)
                or not isinstance(repository_id, int)
                or repository_id < 1
            ):
                raise ValueError
            credential = await self._token_provider.issue(repository_id, CHECK_RUN_PERMISSIONS)
            return credential.value
        except Exception:  # noqa: BLE001 -- credential details must not escape
            raise RuntimeError("GitHub authentication failed") from None


def _check_identity(payload: dict[str, Any]) -> GitHubCheckRun:
    remote_id = payload.get("id")
    head_sha = payload.get("head_sha")
    external_id = payload.get("external_id")
    if isinstance(remote_id, bool) or not isinstance(remote_id, int) or remote_id < 1:
        raise TypeError("GitHub returned an invalid check run identity")
    if not isinstance(head_sha, str) or _SHA.fullmatch(head_sha) is None:
        raise TypeError("GitHub returned an invalid check run commit")
    if not isinstance(external_id, str) or not external_id:
        raise TypeError("GitHub returned an invalid check run external ID")
    return GitHubCheckRun(remote_id, head_sha, external_id)


def _require_owned(payload: dict[str, Any], app_id: int) -> None:
    app = payload.get("app")
    if not isinstance(app, dict) or app.get("id") != app_id:
        raise RuntimeError("GitHub returned a check run owned by another App")


def _require_matching_identity(check: GitHubCheckRun, payload: dict[str, object]) -> None:
    if check.head_sha != payload.get("head_sha") or check.external_id != payload.get("external_id"):
        raise RuntimeError("GitHub returned an unexpected check run")


def _encoded_repository(repository: str) -> str:
    if _REPOSITORY.fullmatch(repository) is None:
        raise ValueError("invalid GitHub repository name")
    owner, name = repository.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise ValueError("invalid GitHub repository name")
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _require_sha(value: str) -> None:
    if _SHA.fullmatch(value) is None:
        raise ValueError("invalid Git commit SHA")


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
