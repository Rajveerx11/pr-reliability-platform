"""Login abuse and concurrent administrator revocation cannot lock out everyone."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pr_reliability_api.auth.sessions import Principal

from .conftest import OWNER, csrf, login


def test_one_client_cannot_exhaust_global_logins_or_spoof_forwarding(environment):
    e = environment
    attacker = TestClient(e.app, base_url=e.settings.origin, client=("192.0.2.1", 1234))
    for index in range(20):
        response = attacker.get(
            "/auth/login",
            follow_redirects=False,
            headers={"X-Forwarded-For": f"198.51.100.{index}"},
        )
        assert response.status_code == 303
    denied = attacker.get("/auth/login", follow_redirects=False)
    assert denied.status_code == 429 and denied.headers["retry-after"] == "300"
    other = TestClient(e.app, base_url=e.settings.origin, client=("192.0.2.2", 1234))
    assert other.get("/auth/login", follow_redirects=False).status_code == 303
    with e.database() as connection:
        assert connection.execute("SELECT count(*) FROM github_login_attempts").fetchone()[0] == 21
        assert (
            connection.execute("SELECT max(attempts) FROM github_login_limits").fetchone()[0] == 20
        )
        hashes = connection.execute("SELECT client_hash FROM github_login_limits").fetchall()
        assert all("192.0.2" not in row[0] for row in hashes)
    e.clock[0] += timedelta(minutes=6)
    assert attacker.get("/auth/login", follow_redirects=False).status_code == 303


def test_completed_logins_do_not_reset_client_budget(environment):
    e = environment
    login(e)
    with e.database() as connection:
        connection.execute("UPDATE github_login_limits SET attempts = 20")
    assert e.client.get("/auth/login", follow_redirects=False).status_code == 429


def test_concurrent_login_admission_obeys_client_limit(environment):
    e = environment

    def attempt(_):
        # New cookies and ports must not defeat an address-based limit.
        with TestClient(e.app, base_url=e.settings.origin, client=("192.0.2.3", 3456)) as client:
            return client.get("/auth/login", follow_redirects=False).status_code

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(25)))
    assert results.count(303) == 20 and results.count(429) == 5


def test_last_admin_cannot_revoke_self(environment):
    e = environment
    e.provider.user_id = 12
    login(e)
    response = e.client.put(
        "/auth/users/12/access", json={"enabled": False}, headers=csrf(e.client)
    )
    assert response.status_code == 409
    assert e.client.get("/auth/session").status_code == 200


def test_simultaneous_cross_revocations_leave_an_enabled_admin(environment):
    e = environment
    e.sessions.settings = replace(e.settings, admin_ids=frozenset({12, 13}))
    for user in [12, 13]:
        e.provider.user_id = user
        login(e)
    with e.database() as connection:
        rows = connection.execute("SELECT github_user_id, actor_id FROM github_users").fetchall()
    principals = {user: Principal(OWNER, actor, user, "admin", "admin", []) for user, actor in rows}

    def revoke(pair):
        actor, target = pair
        try:
            e.sessions.revoke(principals[actor], target, False)
            return 204
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(revoke, [(12, 13), (13, 12)]))
    assert sorted(results) == [204, 403]
    with e.database() as connection:
        assert (
            connection.execute("SELECT count(*) FROM github_users WHERE enabled").fetchone()[0] == 1
        )
