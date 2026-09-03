"""Short-lived, repository-scoped GitHub App installation credentials."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_GITHUB_API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"
_APP_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_PERMISSION = re.compile(r"^[a-z_]{1,64}$")
_MAX_PRIVATE_KEY_BYTES = 1024 * 1024
_MAX_TOKEN_BYTES = 16 * 1024
_MIN_TOKEN_LIFETIME = timedelta(seconds=30)
_MAX_TOKEN_LIFETIME = timedelta(minutes=65)

CHECKOUT_PERMISSIONS = {"contents": "read", "metadata": "read"}
REVIEW_PERMISSIONS = {
    "contents": "read",
    "metadata": "read",
    "pull_requests": "write",
}


class GitHubAppAuthenticationError(RuntimeError):
    """GitHub App authentication failed without exposing provider details."""


@dataclass(frozen=True, slots=True)
class GitHubInstallationToken:
    """Validated installation credential with a deliberately redacted representation."""

    value: str = field(repr=False)
    expires_at: datetime
    repository_id: int
    permission_items: tuple[tuple[str, str], ...]

    @property
    def permissions(self) -> dict[str, str]:
        return dict(self.permission_items)


class GitHubAppInstallationTokenProvider:
    """Mint least-privilege installation tokens for exactly one repository."""

    __slots__ = (
        "_app_id",
        "_installation_id",
        "_now",
        "_private_key",
        "_timeout_seconds",
        "_transport",
    )

    def __init__(
        self,
        app_id: str | int,
        installation_id: int,
        private_key: bytes,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if isinstance(app_id, bool) or (isinstance(app_id, int) and app_id < 1):
            raise ValueError("GitHub App ID is invalid")
        normalized_app_id = str(app_id).strip()
        if _APP_IDENTIFIER.fullmatch(normalized_app_id) is None:
            raise ValueError("GitHub App ID is invalid")
        _require_positive_integer(installation_id, "GitHub installation ID")
        if not isinstance(private_key, bytes) or not private_key:
            raise ValueError("GitHub private key is required")
        if len(private_key) > _MAX_PRIVATE_KEY_BYTES:
            raise ValueError("GitHub private key is too large")
        if timeout_seconds <= 0:
            raise ValueError("GitHub authentication timeout must be positive")
        try:
            loaded_key = serialization.load_pem_private_key(private_key, password=None)
        except (TypeError, ValueError):
            raise ValueError("GitHub private key is invalid") from None
        if not isinstance(loaded_key, rsa.RSAPrivateKey):
            raise TypeError("GitHub private key must be RSA")

        self._app_id = normalized_app_id
        self._installation_id = installation_id
        self._private_key = loaded_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._now = now

    async def issue(
        self,
        repository_id: int,
        permissions: Mapping[str, str],
    ) -> GitHubInstallationToken:
        """Return one fresh token scoped to the requested repository and permissions."""

        _require_positive_integer(repository_id, "GitHub repository ID")
        requested_permissions = _normalize_permissions(permissions)
        now = _utc_now(self._now)
        app_jwt = self._create_jwt(now)
        try:
            async with httpx.AsyncClient(
                base_url=_GITHUB_API_URL + "/",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {app_jwt}",
                    "User-Agent": "pr-reliability-platform",
                    "X-GitHub-Api-Version": _API_VERSION,
                },
                follow_redirects=False,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"app/installations/{self._installation_id}/access_tokens",
                    json={
                        "repository_ids": [repository_id],
                        "permissions": requested_permissions,
                    },
                )
        except Exception:  # noqa: BLE001 -- httpx transports expose arbitrary details
            raise GitHubAppAuthenticationError("GitHub installation token request failed") from None
        if response.status_code != 201:
            raise GitHubAppAuthenticationError("GitHub installation token request failed")
        try:
            payload = response.json()
            return _validated_token(payload, repository_id, requested_permissions, now)
        except GitHubAppAuthenticationError:
            raise
        except Exception:  # noqa: BLE001 -- never retain malformed response details
            raise GitHubAppAuthenticationError(
                "GitHub returned an invalid installation token"
            ) from None

    def _create_jwt(self, now: datetime) -> str:
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "exp": int((now + timedelta(minutes=9)).timestamp()),
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "iss": self._app_id,
        }
        encoded_header = _base64url(_canonical_json(header))
        encoded_claims = _base64url(_canonical_json(claims))
        signing_input = encoded_header + b"." + encoded_claims
        signature = self._private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return (signing_input + b"." + _base64url(signature)).decode("ascii")


def _validated_token(
    payload: Any,
    repository_id: int,
    requested_permissions: dict[str, str],
    now: datetime,
) -> GitHubInstallationToken:
    if not isinstance(payload, dict):
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation token")
    token = payload.get("token")
    if (
        not isinstance(token, str)
        or not token.strip()
        or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
        or "\x00" in token
    ):
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation token")
    expires_at = _parse_expiry(payload.get("expires_at"))
    lifetime = expires_at - now
    if lifetime < _MIN_TOKEN_LIFETIME or lifetime > _MAX_TOKEN_LIFETIME:
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation token")
    returned_permissions = _normalize_permissions(payload.get("permissions"))
    if returned_permissions != requested_permissions:
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation token")
    repositories = payload.get("repositories")
    if not isinstance(repositories, list) or len(repositories) != 1:
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation token")
    repository = repositories[0]
    if not isinstance(repository, dict) or repository.get("id") != repository_id:
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation token")
    return GitHubInstallationToken(
        value=token,
        expires_at=expires_at,
        repository_id=repository_id,
        permission_items=tuple(sorted(returned_permissions.items())),
    )


def _parse_expiry(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation token")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise GitHubAppAuthenticationError(
            "GitHub returned an invalid installation token"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation token")
    return parsed.astimezone(UTC)


def _normalize_permissions(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value or len(value) > 16:
        raise ValueError("GitHub token permissions are invalid")
    normalized: dict[str, str] = {}
    for raw_name, raw_level in value.items():
        if not isinstance(raw_name, str) or _PERMISSION.fullmatch(raw_name) is None:
            raise ValueError("GitHub token permissions are invalid")
        if raw_level not in {"read", "write"}:
            raise ValueError("GitHub token permissions are invalid")
        normalized[raw_name] = raw_level
    return dict(sorted(normalized.items()))


def _require_positive_integer(value: int, subject: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{subject} must be positive")


def _utc_now(now: Callable[[], datetime]) -> datetime:
    value = now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("current time must include a timezone")
    return value.astimezone(UTC)


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")


def _base64url(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")
