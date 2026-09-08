"""HTTP boundary tests for GitHub account/user authorization and secret handling."""

from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pr_reliability_api.auth.github import GitHubIdentity
from pr_reliability_api.auth.settings import LoginSettings, from_environment


@pytest.fixture
def settings():
    return LoginSettings(
        "01J00000000000000000000001",
        7,
        71,
        "client",
        "client-secret",
        Fernet.generate_key().decode(),
        "https://reviews.test",
        frozenset({11}),
        frozenset({12}),
    )


def payload(path):
    if path == "/user":
        return {"id": 11, "login": "reviewer"}
    if path == "/user/installations":
        return {"installations": [{"id": 7, "account": {"id": 71}, "suspended_at": None}]}
    return {"repositories": [{"id": 91}]}


def test_exchange_sends_secret_and_pkce_only_in_post_body(settings, caplog):
    def handler(request):
        assert request.method == "POST"
        assert request.url == "https://github.com/login/oauth/access_token"
        body = parse_qs(request.content.decode())
        assert body["client_secret"] == ["client-secret"]
        assert body["code_verifier"] == ["verifier"]
        return httpx.Response(
            200, json={"access_token": "private-provider-token", "expires_in": 30000}
        )

    provider = GitHubIdentity(settings, transport=httpx.MockTransport(handler))
    token = provider.exchange("code", "verifier")
    assert token.expires_in == 28800
    assert "private-provider-token" not in repr(token)
    assert "client-secret" not in repr(settings)
    assert "private-provider-token" not in caplog.text


@pytest.mark.parametrize(
    "path,change",
    [
        ("/user", {"id": 99, "login": "outsider"}),
        ("/user", {"id": "11", "login": "reviewer"}),
        ("/user/installations", {"installations": [{"id": 8, "account": {"id": 71}}]}),
        ("/user/installations", {"installations": [{"id": 7, "account": {"id": 72}}]}),
        (
            "/user/installations",
            {"installations": [{"id": 7, "account": {"id": 71}, "suspended_at": "today"}]},
        ),
    ],
)
def test_wrong_user_account_installation_or_suspension_denied(settings, path, change):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer private-provider-token"
        assert "private-provider-token" not in str(request.url)
        return httpx.Response(
            200, json=change if request.url.path == path else payload(request.url.path)
        )

    with pytest.raises(HTTPException) as error:
        GitHubIdentity(settings, transport=httpx.MockTransport(handler)).access(
            "private-provider-token"
        )
    assert error.value.status_code == 403


def test_paginated_installations_and_repositories(settings):
    def handler(request):
        page = int(request.url.params.get("page", 1))
        path = request.url.path
        if path == "/user/installations" and page == 1:
            return httpx.Response(
                200, json={"installations": [{"id": i + 1000} for i in range(100)]}
            )
        if path.endswith("/repositories") and page == 1:
            return httpx.Response(
                200, json={"repositories": [{"id": i + 1000} for i in range(100)]}
            )
        return httpx.Response(200, json=payload(path))

    access = GitHubIdentity(settings, transport=httpx.MockTransport(handler)).access("token")
    assert access.user_id == 11 and len(access.repository_ids) == 101
    assert 91 in access.repository_ids


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (403, {"secret": "private-provider-token"}, 403),
        (429, {}, 503),
        (500, {}, 503),
        (200, [], 503),
        (200, {"repositories": "bad"}, 403),
    ],
)
def test_malformed_or_failed_provider_is_sanitized(settings, status, body, expected, caplog):
    provider = GitHubIdentity(
        settings, transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    )
    with pytest.raises(HTTPException) as error:
        provider.access("private-provider-token")
    assert error.value.status_code == expected
    assert "private-provider-token" not in str(error.value) + caplog.text


def test_transport_timeout_does_not_leak_provider_error(settings):
    def handler(request):
        raise httpx.ReadTimeout("private-provider-token", request=request)

    with pytest.raises(HTTPException) as error:
        GitHubIdentity(settings, transport=httpx.MockTransport(handler)).access(
            "private-provider-token"
        )
    assert error.value.status_code == 503
    assert "private-provider-token" not in str(error.value)


@pytest.mark.parametrize(
    "origin",
    [
        "http://reviews.test",
        "https://reviews.test/",
        "https://user@reviews.test",
        "https://reviews.test?x=1",
        "https://reviews.test/#x",
    ],
)
def test_origin_must_be_fixed_https_origin(settings, origin):
    with pytest.raises(ValueError):
        replace(settings, origin=origin)


def test_production_configuration_requires_github_not_shared_token():
    with pytest.raises(ValueError, match="GITHUB_ALLOWED_ACCOUNT_ID"):
        from_environment(
            "01J00000000000000000000001",
            7,
            {"APPROVAL_ACTOR_ID": "x", "APPROVAL_REVIEWER_TOKEN": "old-token"},
        )
