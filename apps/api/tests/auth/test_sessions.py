"""Login lifecycle, session storage, CSRF, and live access regression tests."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pr_reliability_api.auth.sessions import (
    LOGIN_COOKIE,
    LOGIN_RETURN_COOKIE,
    SESSION_COOKIE,
    digest,
)

from .conftest import OWNER, csrf, decision, login


def test_login_pkce_storage_cookies_and_replay(environment):
    e = environment
    response, callback, query = login(e)
    assert query["code_challenge_method"] == ["S256"]
    assert "test-provider-token" not in response.headers["location"]
    assert "client_secret" not in response.headers["location"]
    for header in callback.headers.get_list("set-cookie"):
        assert "HttpOnly" in header and "Secure" in header and "SameSite=lax" in header
        assert "Path=/" in header and "Domain" not in header
    raw = e.client.cookies.get(SESSION_COOKIE)
    with e.database() as connection:
        row = connection.execute(
            "SELECT token_hash, encrypted_token FROM browser_sessions"
        ).fetchone()
        assert row[0] == digest(raw) and raw != row[0]
        assert "test-provider-token" not in row[1]
    assert e.client.cookies.get(LOGIN_COOKIE) is None
    replay = e.client.get(
        "/auth/callback",
        params={"state": query["state"][0], "code": "valid-code"},
        follow_redirects=False,
    )
    assert replay.headers["location"] == "/dashboard?login=failed"
    assert e.provider.exchanges == 1
    assert e.client.get("/auth/session").json()["github_user_id"] == 11


def test_login_preserves_valid_check_run_deep_link(environment):
    from urllib.parse import parse_qs, urlsplit

    run_id = "01J00000000000000000000003"
    assert environment.client.get("/auth/login", follow_redirects=False).status_code == 303
    response = environment.client.get("/auth/login", params={"run": run_id}, follow_redirects=False)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    callback = environment.client.get(
        "/auth/callback",
        params={"state": query["state"][0], "code": "valid-code"},
        follow_redirects=False,
    )

    assert callback.headers["location"] == f"/dashboard?run={run_id}"
    assert environment.client.cookies.get(LOGIN_RETURN_COOKIE) is None
    assert (
        environment.client.get(
            "/auth/login", params={"run": "../outside"}, follow_redirects=False
        ).status_code
        == 422
    )


def test_callback_is_browser_bound_and_expires(environment):
    from urllib.parse import parse_qs, urlsplit

    e = environment
    response = e.client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    stranger = TestClient(e.app, base_url=e.settings.origin)
    assert (
        stranger.get(
            "/auth/callback", params={"state": state, "code": "valid-code"}, follow_redirects=False
        )
        .headers["location"]
        .endswith("login=failed")
    )
    e.clock[0] += timedelta(minutes=6)
    assert (
        e.client.get(
            "/auth/callback", params={"state": state, "code": "valid-code"}, follow_redirects=False
        )
        .headers["location"]
        .endswith("login=failed")
    )
    assert e.provider.exchanges == 0


def test_rotation_expiry_and_logout(environment):
    e = environment
    login(e)
    raw = e.client.cookies.get(SESSION_COOKIE)
    headers = csrf(e.client)
    assert e.client.post("/auth/session/rotate", headers=headers).status_code == 204
    rotated = e.client.cookies.get(SESSION_COOKIE)
    assert rotated != raw
    stale = TestClient(e.app, base_url=e.settings.origin)
    stale.cookies.set(SESSION_COOKIE, raw)
    assert stale.get("/auth/session").status_code == 401
    assert e.client.post("/auth/logout", headers=headers).status_code == 403
    headers = csrf(e.client)
    e.provider.unavailable = True
    assert e.client.post("/auth/logout", headers=headers).status_code == 204
    assert e.client.cookies.get(SESSION_COOKIE) is None
    e.provider.unavailable = False
    login(e)
    e.clock[0] += timedelta(hours=9)
    assert e.client.get("/auth/session").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.test", "X-CSRF-Token": "x"},
        {"Origin": "https://reviews.test", "X-CSRF-Token": "x"},
    ],
)
def test_mutations_require_csrf_and_exact_origin(environment, headers):
    e = environment
    login(e)
    for path in ["/auth/logout", "/auth/session/rotate"]:
        assert e.client.post(path, headers=headers).status_code == 403
    assert (
        e.client.post(
            f"/api/approval-inbox/{e.visible.finding}/decision", json=decision(), headers=headers
        ).status_code
        == 403
    )
    with e.database() as connection:
        assert connection.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


def test_live_access_removed_and_provider_failure_deny_requests(environment):
    e = environment
    login(e)
    assert e.client.get("/api/dashboard/overview").status_code == 200
    e.provider.denied = True
    for path in ["/api/dashboard/overview", "/api/approval-inbox", "/api/repositories"]:
        assert e.client.get(path).status_code == 403
    e.provider.denied = False
    e.provider.unavailable = True
    assert e.client.get("/api/dashboard/overview").status_code == 503
    e.provider.unavailable = False
    e.provider.user_id = 12
    assert e.client.get("/auth/session").status_code == 403


def test_individual_revocation_and_roles(environment):
    e = environment
    login(e)
    headers = csrf(e.client)
    assert (
        e.client.put("/auth/users/11/access", json={"enabled": False}, headers=headers).status_code
        == 403
    )
    admin = TestClient(e.app, base_url=e.settings.origin)
    e.provider.user_id = 12
    login(e, admin)
    admin_headers = csrf(admin)
    assert (
        admin.put(
            "/auth/users/11/access", json={"enabled": False}, headers=admin_headers
        ).status_code
        == 204
    )
    assert admin.get("/auth/session").status_code == 200
    e.provider.user_id = 11
    assert e.client.get("/auth/session").status_code == 401
    response = e.client.get("/auth/login", follow_redirects=False)
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    assert (
        e.client.get(
            "/auth/callback", params={"state": state, "code": "valid-code"}, follow_redirects=False
        )
        .headers["location"]
        .endswith("login=failed")
    )
    with e.database() as connection:
        event = connection.execute(
            "SELECT event_data FROM repository_events WHERE event_type = 'user.access_changed'"
        ).fetchone()[0]
        assert event["github_user_id"] == 12 and event["target_github_user_id"] == 11
        assert (
            connection.execute(
                "SELECT count(*) FROM browser_sessions WHERE owner_id = %s AND github_user_id = 12",
                (OWNER,),
            ).fetchone()[0]
            == 1
        )


def test_production_session_routes_reject_legacy_token(environment):
    for path in [
        "/auth/session",
        "/api/dashboard/overview",
        "/api/approval-inbox",
        "/api/repositories",
    ]:
        response = environment.client.get(
            path, headers={"Authorization": "Bearer legacy-test-token"}
        )
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"


def test_local_logout_proof_survives_github_outage(environment):
    e = environment
    login(e)
    e.provider.unavailable = True
    assert e.client.get("/auth/session").status_code == 503
    local = e.client.get("/auth/logout-token")
    assert local.status_code == 200 and set(local.json()) == {"csrf_token"}
    headers = {"Origin": e.settings.origin, "X-CSRF-Token": local.json()["csrf_token"]}
    assert e.client.post("/auth/logout", headers=headers).status_code == 204
    assert e.client.get("/auth/logout-token").status_code == 401


def test_reenable_requires_fresh_login_and_preserves_actor(environment):
    e = environment
    login(e)
    old = e.client.cookies.get(SESSION_COOKIE)
    with e.database() as connection:
        actor = connection.execute(
            "SELECT actor_id FROM github_users WHERE github_user_id = 11"
        ).fetchone()[0]
    admin = TestClient(e.app, base_url=e.settings.origin)
    e.provider.user_id = 12
    login(e, admin)
    headers = csrf(admin)
    for enabled in [False, True]:
        assert (
            admin.put(
                "/auth/users/11/access", json={"enabled": enabled}, headers=headers
            ).status_code
            == 204
        )
    e.provider.user_id = 11
    assert e.client.get("/auth/session").status_code == 401
    login(e)
    assert e.client.cookies.get(SESSION_COOKIE) != old
    with e.database() as connection:
        assert (
            connection.execute(
                "SELECT actor_id FROM github_users WHERE github_user_id = 11"
            ).fetchone()[0]
            == actor
        )


def test_fresh_login_and_concurrent_rotation_invalidate_old_bearers(environment):
    from concurrent.futures import ThreadPoolExecutor

    e = environment
    login(e)
    first = e.client.cookies.get(SESSION_COOKIE)
    login(e)
    raw = e.client.cookies.get(SESSION_COOKIE)
    assert first != raw
    stale = TestClient(e.app, base_url=e.settings.origin)
    stale.cookies.set(SESSION_COOKIE, first)
    assert stale.get("/auth/session").status_code == 401
    headers = csrf(e.client)

    def rotate():
        with TestClient(e.app, base_url=e.settings.origin) as client:
            client.cookies.set(SESSION_COOKIE, raw)
            return client.post("/auth/session/rotate", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: rotate(), range(2)))
    assert sorted(results) == [204, 401]
    assert e.client.get("/auth/session").status_code == 401
