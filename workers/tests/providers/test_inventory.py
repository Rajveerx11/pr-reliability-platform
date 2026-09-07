"""Metadata-only inventory, bounded pagination, and safe provider failures."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pr_reliability_workers.providers.inventory import InventoryError, fetch_inventory

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def repo(number):
    return {"id": number, "full_name": f"owner/repo-{number}", "default_branch": "main"}


def fetch(handler):
    return asyncio.run(fetch_inventory(71, "test-jwt", NOW, transport=httpx.MockTransport(handler)))


def handler_for(pages, *, status=200, token_changes=None):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.github.com"
        if request.url.path == "/app/installations/71":
            assert request.headers["Authorization"] == "Bearer test-jwt"
            return httpx.Response(status, json={"id": 71, "suspended_at": None})
        if request.url.path.endswith("/access_tokens"):
            assert json.loads(request.content) == {"permissions": {"metadata": "read"}}
            return httpx.Response(
                201,
                json={
                    "token": "test-inventory-token",
                    "permissions": {"metadata": "read"},
                    "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                    **(token_changes or {}),
                },
            )
        assert request.url.path == "/installation/repositories"
        assert request.headers["Authorization"] == "Bearer test-inventory-token"
        return httpx.Response(200, json=pages[int(request.url.params["page"]) - 1])

    return handler, requests


def test_pages_import_all_selected_repositories():
    handler, requests = handler_for(
        [
            {"total_count": 101, "repositories": [repo(n) for n in range(1, 101)]},
            {"total_count": 101, "repositories": [repo(101)]},
        ]
    )
    value = fetch(handler)
    assert len(value.repositories) == 101
    assert value.installation_id == 71 and value.state == "active"
    assert len(requests) == 4


@pytest.mark.parametrize(
    "pages",
    [
        [{"total_count": 2, "repositories": [repo(1)]}],
        [{"total_count": 2, "repositories": [repo(1), repo(1)]}],
        [{"total_count": True, "repositories": []}],
        [{"total_count": 10_001, "repositories": []}],
        [{"total_count": 1, "repositories": [{"id": 1, "full_name": "../../secret"}]}],
        [
            {"total_count": 101, "repositories": [repo(n) for n in range(1, 101)]},
            {"total_count": 100, "repositories": []},
        ],
    ],
)
def test_partial_changing_or_malformed_inventory_is_rejected(pages):
    handler, _ = handler_for(pages)
    with pytest.raises(InventoryError, match="GitHub inventory synchronization failed"):
        fetch(handler)


@pytest.mark.parametrize(
    "changes",
    [
        {"permissions": {"metadata": "read", "contents": "write"}},
        {"expires_at": NOW.isoformat()},
        {"token": ""},
    ],
)
def test_invalid_or_overprivileged_token_is_rejected(changes):
    handler, requests = handler_for([], token_changes=changes)
    with pytest.raises(InventoryError):
        fetch(handler)
    assert len(requests) == 2


def test_missing_installation_revokes_without_token():
    handler, requests = handler_for([], status=404)
    assert fetch(handler).state == "deleted"
    assert len(requests) == 1


def test_suspended_installation_revokes_without_token():
    def handler(request):
        assert request.url.path == "/app/installations/71"
        return httpx.Response(200, json={"id": 71, "suspended_at": NOW.isoformat()})

    assert fetch(handler).state == "suspended"


def test_transport_failure_redacts_credentials():
    def handler(request):
        raise RuntimeError("secret-token-and-source")

    with pytest.raises(InventoryError) as caught:
        fetch(handler)
    assert "secret" not in str(caught.value)


def test_redirect_is_not_followed():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.invalid"})

    with pytest.raises(InventoryError):
        fetch(handler)
    assert len(requests) == 1
