"""Bounded GitHub App user authorization. Never expose provider error bodies."""

import hashlib
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import Lock

import httpx
from fastapi import HTTPException


@dataclass(frozen=True)
class Access:
    user_id: int
    login: str
    repository_ids: frozenset[int]


@dataclass(frozen=True)
class Token:
    value: str = field(repr=False)
    expires_in: int = 28800


class GitHubIdentity:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport
        self._lock = Lock()
        self._inflight: dict[str, Future[Access]] = {}

    def _request(self, method, url, **kwargs):
        try:
            with httpx.Client(
                timeout=10, follow_redirects=False, transport=self.transport
            ) as client:
                response = client.request(method, url, **kwargs)
            if response.status_code in {401, 403, 404}:
                raise HTTPException(403, "GitHub access denied")
            if response.status_code != 200 or len(response.content) > 2_000_000:
                raise HTTPException(503, "GitHub identity service unavailable")
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError()
            return data
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(503, "GitHub identity service unavailable") from None

    def exchange(self, code, verifier):
        data = self._request(
            "POST",
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "redirect_uri": self.settings.callback,
                "code": code,
                "code_verifier": verifier,
            },
        )
        value = data.get("access_token")
        expiry = data.get("expires_in", 28800)
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 4096
            or type(expiry) is not int
            or expiry <= 0
        ):
            raise HTTPException(403, "GitHub login failed")
        return Token(value, min(expiry, 28800))

    def access(self, token):
        # Only overlapping checks share a result. Completed permissions are never cached.
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            pending = self._inflight.get(key)
            leader = pending is None
            if leader:
                pending = self._inflight[key] = Future()
        if not leader:
            return pending.result()
        try:
            result = self._access(token)
        except BaseException as error:
            with self._lock:
                del self._inflight[key]
                pending.set_exception(error)
            raise
        else:
            with self._lock:
                del self._inflight[key]
                pending.set_result(result)
            return result

    def _access(self, token):
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        user = self._request("GET", "https://api.github.com/user", headers=headers)
        user_id, login = user.get("id"), user.get("login")
        if (
            type(user_id) is not int
            or not self.settings.role(user_id)
            or not isinstance(login, str)
            or not 1 <= len(login) <= 100
        ):
            raise HTTPException(403, "GitHub user is not allowed")
        # Enumerate the user's installations, never trust an installation ID from the browser.
        found = False
        for page in range(1, 101):
            data = self._request(
                "GET",
                "https://api.github.com/user/installations",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            items = data.get("installations")
            if not isinstance(items, list):
                raise HTTPException(503, "GitHub identity service unavailable")
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("id") == self.settings.installation_id
                    and isinstance(item.get("account"), dict)
                    and item["account"].get("id") == self.settings.account_id
                    and not item.get("suspended_at")
                ):
                    found = True
            if found or len(items) < 100:
                break
        if not found:
            raise HTTPException(403, "GitHub installation access denied")
        repositories = set()
        for page in range(1, 101):
            data = self._request(
                "GET",
                "https://api.github.com/user/installations/"
                f"{self.settings.installation_id}/repositories",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            items = data.get("repositories")
            if not isinstance(items, list):
                raise HTTPException(503, "GitHub identity service unavailable")
            for item in items:
                if not isinstance(item, dict) or type(item.get("id")) is not int:
                    raise HTTPException(503, "GitHub identity service unavailable")
                repositories.add(item["id"])
            if len(items) < 100:
                return Access(user_id, login, frozenset(repositories))
        raise HTTPException(503, "GitHub repository inventory exceeds login limit")
