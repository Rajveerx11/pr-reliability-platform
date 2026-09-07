"""Repository lifecycle, reconciliation, policy, and review admission integration."""

import asyncio
import json

import pytest
import test_github_webhooks as webhook_tests
from fastapi.testclient import TestClient
from pr_reliability_api.app import create_app
from pr_reliability_api.approvals import ApprovalInboxSettings
from pr_reliability_api.repositories.store import apply_snapshot
from pr_reliability_api.webhooks import GithubWebhookSettings
from pr_reliability_contracts.repositories import InstallationSnapshot
from pr_reliability_workers.repository_sync import reconcile_once
from psycopg.errors import ObjectNotInPrerequisiteState
from test_github_webhooks import (
    OWNER_ID,
    SECRET,
    headers,
    payload,
    post,
    scalar,
)

client = webhook_tests.client
connection_factory = webhook_tests.connection_factory
database_url = webhook_tests.database_url

OTHER_OWNER = "01J00000000000000000000002"
AUTH = {"Authorization": "Bearer reviewer-token"}


def snapshot(ids=(91,), *, state="active"):
    return InstallationSnapshot(
        installation_id=71,
        state=state,
        repositories=[
            {"id": number, "full_name": f"owner/repo-{number}", "default_branch": "main"}
            for number in ids
        ],
    )


def sync(factory, value, *, owner=OWNER_ID):
    with factory() as connection:
        row = connection.execute(
            "SELECT revision FROM github_installations WHERE owner_id = %s AND installation_id=71",
            (owner,),
        ).fetchone()
        return apply_snapshot(connection, owner, value, row[0] if row else 0)


def lifecycle(client, action, *, removed=(), added=(), delivery=None, installation_id=71):
    event = "installation_repositories" if action in {"added", "removed"} else "installation"
    data = {"installation": {"id": installation_id}, "action": action}
    if event == "installation_repositories":
        data.update(
            repositories_added=[{"id": n} for n in added],
            repositories_removed=[{"id": n} for n in removed],
        )
    body = json.dumps(data).encode()
    request_headers = headers(body, delivery=delivery or action)
    request_headers["X-GitHub-Event"] = event
    return client.post("/webhooks/github", content=body, headers=request_headers)


@pytest.fixture
def api(connection_factory):
    return TestClient(
        create_app(
            GithubWebhookSettings(OWNER_ID, 71, SECRET),
            connection_factory,
            ApprovalInboxSettings(OWNER_ID, "reviewer", "reviewer-token"),
        )
    )


def policy(api, **changes):
    repo = api.get("/api/repositories", headers=AUTH).json()["repositories"][0]
    value = {
        "schema_version": "1",
        "enabled": True,
        "enabled_branches": ["main"],
        "token_budget": 2345,
        "cost_budget_usd_micros": 678,
        "verification_profile": "default",
        **changes,
    }
    return api.put(f"/api/repositories/{repo['public_id']}/policy", json=value, headers=AUTH)


def test_initial_inventory_import_and_repeat_preserve_identity_and_policy(api, connection_factory):
    assert sync(connection_factory, snapshot((91, 92)))
    first = api.get("/api/repositories", headers=AUTH).json()["repositories"]
    assert len(first) == 2
    assert all(r["default_branch"] == "main" and r["last_sync_at"] for r in first)
    assert policy(api, enabled=False).status_code == 200
    assert sync(connection_factory, snapshot((91, 92)))
    second = api.get("/api/repositories", headers=AUTH).json()["repositories"]
    assert [r["public_id"] for r in first] == [r["public_id"] for r in second]
    assert second[0]["enabled"] is False
    assert second[0]["token_budget"] == 2345


def test_policy_budgets_reach_run_and_command(client, api, connection_factory):
    assert policy(api).status_code == 200
    response = post(client, payload())
    assert response.json()["command_id"]
    with connection_factory() as connection:
        assert connection.execute(
            "SELECT token_budget, cost_budget_usd_micros, base_branch, verification_profile FROM runs"
        ).fetchone() == (2345, 678, "main", "default")
        command = connection.execute("SELECT event_data FROM run_events").fetchone()[0]
        assert (command["token_budget"], command["cost_budget_usd_micros"]) == (2345, 678)
    repo = api.get("/api/repositories", headers=AUTH).json()["repositories"][0]
    assert repo["last_webhook_at"] and repo["last_review_run_at"]


@pytest.mark.parametrize("changes", [{"enabled": False}, {"enabled_branches": ["release"]}])
def test_policy_blocks_new_reviews(client, api, connection_factory, changes):
    assert policy(api, **changes).status_code == 200
    assert post(client, payload()).json()["command_id"] is None
    assert scalar(connection_factory, "SELECT count(*) FROM runs") == 0


def test_missing_branch_cannot_bypass_filter(client, api):
    assert policy(api).status_code == 200
    data = payload()
    data["pull_request"]["base"].pop("ref")
    assert post(client, data).json()["command_id"] is None


@pytest.mark.parametrize("action", ["suspend", "deleted", "removed"])
def test_revocation_replay_and_delayed_restore_fail_closed(client, connection_factory, action):
    response = lifecycle(client, action, removed=(91,))
    assert response.status_code == 200
    assert lifecycle(client, action, removed=(91,)).json()["duplicate"] is True
    lifecycle(client, "unsuspend")
    lifecycle(client, "added", added=(91,))
    assert post(client, payload()).json()["command_id"] is None
    assert sync(connection_factory, snapshot())
    assert post(
        client, payload("reopened", updated_at="2026-08-14T00:00:00Z"), delivery="after-sync"
    ).json()["command_id"]


def test_unknown_and_stale_inventory_fail_closed(api, connection_factory):
    assert post(api, payload()).json()["command_id"] is None
    assert sync(connection_factory, snapshot())
    with connection_factory() as connection:
        connection.execute(
            "UPDATE github_installations SET last_sync_at=now()-interval '16 minutes'"
        )
    assert post(api, payload(), delivery="expired").json()["command_id"] is None
    assert scalar(connection_factory, "SELECT count(*) FROM runs") == 0


def test_webhook_during_network_read_fences_snapshot(client, connection_factory):
    class Provider:
        async def inventory(self):
            lifecycle(client, "removed", removed=(91,))
            return snapshot()

    assert asyncio.run(reconcile_once(connection_factory, OWNER_ID, 71, Provider())) is False
    assert post(client, payload()).json()["command_id"] is None


def test_complete_snapshot_removes_missing_repositories(client, connection_factory):
    assert sync(connection_factory, snapshot((92,)))
    assert post(client, payload()).json()["command_id"] is None
    with connection_factory() as connection:
        assert connection.execute(
            "SELECT access_state FROM repositories WHERE github_repository_id=91"
        ).fetchone() == ("removed",)


def test_failed_sync_preserves_inventory(client, connection_factory):
    class Provider:
        async def inventory(self):
            raise RuntimeError("network unavailable")

    with pytest.raises(RuntimeError):
        asyncio.run(reconcile_once(connection_factory, OWNER_ID, 71, Provider()))
    assert post(client, payload()).json()["command_id"]


def test_owner_isolation_authentication_and_pagination(client, api, connection_factory):
    sync(connection_factory, snapshot((92,)), owner=OTHER_OWNER)
    assert api.get("/api/repositories").status_code == 401
    assert api.get("/api/repositories?limit=101", headers=AUTH).status_code == 422
    sync(connection_factory, snapshot((91, 93)))
    first = api.get("/api/repositories?limit=1", headers=AUTH).json()
    second = api.get(f"/api/repositories?limit=1&after={first['next_cursor']}", headers=AUTH).json()
    assert first["next_cursor"] and second["next_cursor"] is None
    assert {
        first["repositories"][0]["github_repository_id"],
        second["repositories"][0]["github_repository_id"],
    } == {91, 93}
    with connection_factory() as connection:
        foreign_id = connection.execute(
            "SELECT public_id FROM repositories WHERE owner_id=%s", (OTHER_OWNER,)
        ).fetchone()[0]
    assert (
        api.put(
            f"/api/repositories/{foreign_id}/policy", json={"schema_version": "1"}, headers=AUTH
        ).status_code
        == 404
    )
    assert lifecycle(client, "deleted", installation_id=72).status_code == 403


def test_policy_is_idempotent_and_audit_is_append_only(client, api, connection_factory):
    assert policy(api).status_code == 200
    assert policy(api).status_code == 200
    assert (
        scalar(
            connection_factory,
            "SELECT count(*) FROM repository_events WHERE event_type='repository.policy_changed'",
        )
        == 1
    )
    with connection_factory() as connection, pytest.raises(ObjectNotInPrerequisiteState):
        connection.execute("DELETE FROM repository_events")


def test_unknown_profile_and_negative_budget_rejected(client, api):
    assert policy(api, verification_profile="host-shell").status_code == 422
    assert policy(api, cost_budget_usd_micros=-1).status_code == 422


def test_equal_count_inventory_replacement_has_repository_audit(client, connection_factory):
    assert sync(connection_factory, snapshot((92,)))
    with connection_factory() as connection:
        changes = connection.execute(
            "SELECT event_type, event_data FROM repository_events WHERE event_key LIKE 'sync:71:2:%'"
        ).fetchall()
        assert ("repository.removed", {"github_repository_id": 91}) in changes
        granted = next(data for kind, data in changes if kind == "repository.synchronized")
        assert granted["github_repository_id"] == 92
        assert granted["before"] is None
        assert granted["after"]["default_branch"] == "main"
    lifecycle(client, "added", added=(93,))
    with connection_factory() as connection:
        data = connection.execute(
            "SELECT event_data FROM repository_events WHERE event_key='webhook:added'"
        ).fetchone()[0]
        assert data["added_repository_ids"] == [93]


def test_rename_and_default_branch_changes_are_audited(client, connection_factory):
    changed = InstallationSnapshot(
        installation_id=71,
        state="active",
        repositories=[{"id": 91, "full_name": "owner/renamed", "default_branch": "release"}],
    )
    sync(connection_factory, changed)
    with connection_factory() as connection:
        data = connection.execute(
            "SELECT event_data FROM repository_events WHERE event_key='sync:71:2:91'"
        ).fetchone()[0]
        assert data["before"]["full_name"] == "owner/repository"
        assert data["after"]["full_name"] == "owner/renamed"
        assert data["after"]["default_branch"] == "release"
