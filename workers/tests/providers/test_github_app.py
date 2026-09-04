"""Tests for repository-scoped GitHub App installation credentials."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pr_reliability_workers.activities import GitHubRestReviewClient
from pr_reliability_workers.providers import (
    CHECKOUT_PERMISSIONS,
    REVIEW_PERMISSIONS,
    GitHubAppAuthenticationError,
    GitHubAppInstallationTokenProvider,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
REPOSITORY_ID = 987654
INSTALLATION_ID = 42
APP_ID = 12345


@pytest.fixture
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def private_key_pem(private_key: rsa.RSAPrivateKey) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def test_mints_signed_single_repository_token_with_least_permissions(
    private_key: rsa.RSAPrivateKey,
    private_key_pem: bytes,
) -> None:
    installation_token = "ghs_1234567890_" + "x" * 180

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == ("https://api.github.com/app/installations/42/access_tokens")
        assert request.headers["x-github-api-version"] == "2022-11-28"
        assert json.loads(request.content) == {
            "repository_ids": [REPOSITORY_ID],
            "permissions": CHECKOUT_PERMISSIONS,
        }
        app_jwt = request.headers["authorization"].removeprefix("Bearer ")
        header, claims, signature = app_jwt.split(".")
        assert _decode_json(header) == {"alg": "RS256", "typ": "JWT"}
        assert _decode_json(claims) == {
            "exp": int((NOW + timedelta(minutes=9)).timestamp()),
            "iat": int((NOW - timedelta(seconds=60)).timestamp()),
            "iss": str(APP_ID),
        }
        private_key.public_key().verify(
            _decode(signature),
            f"{header}.{claims}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return httpx.Response(
            201,
            json={
                "token": installation_token,
                "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                "permissions": CHECKOUT_PERMISSIONS,
                "repositories": [{"id": REPOSITORY_ID, "full_name": "owner/repository"}],
            },
        )

    provider = GitHubAppInstallationTokenProvider(
        APP_ID,
        INSTALLATION_ID,
        private_key_pem,
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
    )

    result = _run(provider.issue(REPOSITORY_ID, CHECKOUT_PERMISSIONS))

    assert result.value == installation_token
    assert result.repository_id == REPOSITORY_ID
    assert result.expires_at == NOW + timedelta(hours=1)
    assert result.permissions == CHECKOUT_PERMISSIONS
    assert installation_token not in repr(result)


def test_review_client_refreshes_repository_scoped_credentials(
    private_key_pem: bytes,
) -> None:
    token_requests: list[dict[str, object]] = []
    resolved: list[str] = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        token_requests.append(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "token": "ghs_review_token",
                "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                "permissions": REVIEW_PERMISSIONS,
                "repositories": [{"id": REPOSITORY_ID}],
            },
        )

    def review_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ghs_review_token"
        return httpx.Response(200, json={"head": {"sha": "a" * 40}})

    provider = GitHubAppInstallationTokenProvider(
        APP_ID,
        INSTALLATION_ID,
        private_key_pem,
        transport=httpx.MockTransport(token_handler),
        now=lambda: NOW,
    )
    client = GitHubRestReviewClient(
        provider,
        99,
        repository_id_resolver=lambda repository: resolved.append(repository) or REPOSITORY_ID,
        transport=httpx.MockTransport(review_handler),
    )

    assert _run(client.current_head_sha("owner/repository", 7)) == "a" * 40
    assert client.uses_installation_token_provider
    assert resolved == ["owner/repository"]
    assert token_requests == [
        {"repository_ids": [REPOSITORY_ID], "permissions": REVIEW_PERMISSIONS}
    ]


@pytest.mark.parametrize(
    ("response", "expected_message"),
    [
        (httpx.Response(302, headers={"location": "https://attacker.invalid"}), "request failed"),
        (httpx.Response(401, text="secret provider body"), "request failed"),
        (httpx.Response(201, text="not-json"), "invalid installation token"),
        (
            httpx.Response(
                201,
                json={
                    "token": "ghs_token",
                    "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                    "permissions": CHECKOUT_PERMISSIONS,
                    "repositories": [{"id": REPOSITORY_ID + 1}],
                },
            ),
            "invalid installation token",
        ),
        (
            httpx.Response(
                201,
                json={
                    "token": "ghs_token",
                    "expires_at": (NOW + timedelta(hours=2)).isoformat(),
                    "permissions": CHECKOUT_PERMISSIONS,
                    "repositories": [{"id": REPOSITORY_ID}],
                },
            ),
            "invalid installation token",
        ),
        (
            httpx.Response(
                201,
                json={
                    "token": "ghs_token",
                    "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                    "permissions": {"contents": "write", "metadata": "read"},
                    "repositories": [{"id": REPOSITORY_ID}],
                },
            ),
            "invalid installation token",
        ),
    ],
)
def test_rejects_redirects_errors_and_scope_escalation_without_leaking_response(
    private_key_pem: bytes,
    response: httpx.Response,
    expected_message: str,
) -> None:
    provider = GitHubAppInstallationTokenProvider(
        APP_ID,
        INSTALLATION_ID,
        private_key_pem,
        transport=httpx.MockTransport(lambda request: response),
        now=lambda: NOW,
    )

    with pytest.raises(GitHubAppAuthenticationError, match=expected_message) as raised:
        _run(provider.issue(REPOSITORY_ID, CHECKOUT_PERMISSIONS))

    assert "secret provider body" not in str(raised.value)
    assert "attacker.invalid" not in str(raised.value)


def test_transport_failure_is_sanitized(private_key_pem: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"transport leaked {request.headers['authorization']}")

    provider = GitHubAppInstallationTokenProvider(
        APP_ID,
        INSTALLATION_ID,
        private_key_pem,
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
    )

    with pytest.raises(GitHubAppAuthenticationError) as raised:
        _run(provider.issue(REPOSITORY_ID, CHECKOUT_PERMISSIONS))

    assert str(raised.value) == "GitHub installation token request failed"
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("app_id", ["", "bad/app", "\x00", -1])
def test_rejects_invalid_app_identifiers(private_key_pem: bytes, app_id: object) -> None:
    with pytest.raises(ValueError, match="App ID"):
        GitHubAppInstallationTokenProvider(
            app_id,  # type: ignore[arg-type]
            INSTALLATION_ID,
            private_key_pem,
        )


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _decode_json(value: str) -> object:
    return json.loads(_decode(value))


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
