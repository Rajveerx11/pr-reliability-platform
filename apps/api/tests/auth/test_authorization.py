"""Repository and owner scope applies to counts, pages, details, and writes."""

from .conftest import csrf, decision, login


def test_lists_counts_details_and_audit_identity(environment):
    e = environment
    login(e)
    overview = e.client.get("/api/dashboard/overview").json()
    assert overview["total_runs"] == overview["pending_findings"] == 1
    runs = e.client.get("/api/dashboard/runs").json()
    assert runs["total"] == 1 and runs["items"][0]["run_id"] == e.visible.run
    assert (
        e.client.get("/api/dashboard/runs", params={"repository": "owner/repo-92"}).json()["total"]
        == 0
    )
    assert len(e.client.get("/api/repositories").json()["repositories"]) == 1
    assert len(e.client.get("/api/approval-inbox").json()) == 1
    assert e.client.get(f"/api/dashboard/runs/{e.visible.run}").status_code == 200
    headers = csrf(e.client)
    for denied in [e.hidden, e.other]:
        assert e.client.get(f"/api/dashboard/runs/{denied.run}").status_code == 404
        assert (
            e.client.post(
                f"/api/approval-inbox/{denied.finding}/decision", json=decision(), headers=headers
            ).status_code
            == 404
        )
    response = e.client.post(
        f"/api/approval-inbox/{e.visible.finding}/decision", json=decision(), headers=headers
    )
    assert response.status_code == 200, response.text
    with e.database() as connection:
        row = connection.execute(
            "SELECT a.actor_id, u.github_user_id FROM approvals a JOIN github_users u ON u.owner_id = a.owner_id AND u.actor_id = a.actor_id"
        ).fetchone()
        assert row[1] == 11
        event = connection.execute(
            "SELECT event_data FROM run_events WHERE event_type = 'approval.decision_recorded'"
        ).fetchone()[0]
        assert event["github_user_id"] == 11 and event["actor_id"] == row[0]
        assert connection.execute("SELECT count(*) FROM external_actions").fetchone()[0] == 0


def test_repo_removal_and_stale_sync_hide_previous_data(environment):
    e = environment
    login(e)
    headers = csrf(e.client)
    e.provider.repositories = frozenset()
    assert e.client.get("/api/dashboard/overview").json()["total_runs"] == 0
    assert e.client.get("/api/approval-inbox").json() == []
    assert (
        e.client.post(
            f"/api/approval-inbox/{e.visible.finding}/decision", json=decision(), headers=headers
        ).status_code
        == 404
    )
    e.provider.repositories = frozenset({91})
    with e.database() as connection:
        connection.execute(
            "UPDATE github_installations SET last_sync_at = now() - interval '1 hour'"
        )
    assert e.client.get("/api/dashboard/runs").json()["total"] == 0


def test_policy_requires_admin_and_repository_access(environment):
    e = environment
    policy = {
        "schema_version": "1",
        "enabled": False,
        "enabled_branches": [],
        "token_budget": 1000,
        "cost_budget_usd_micros": 1000,
        "verification_profile": "default",
    }
    login(e)
    assert (
        e.client.put(
            f"/api/repositories/{e.visible.repository}/policy", json=policy, headers=csrf(e.client)
        ).status_code
        == 403
    )
    e.provider.user_id = 12
    login(e)
    headers = csrf(e.client)
    assert (
        e.client.put(
            f"/api/repositories/{e.hidden.repository}/policy", json=policy, headers=headers
        ).status_code
        == 404
    )
    response = e.client.put(
        f"/api/repositories/{e.visible.repository}/policy", json=policy, headers=headers
    )
    assert response.status_code == 200, response.text


def test_browser_assets_have_login_and_no_token_entry(environment):
    client = environment.client
    for path in ["/dashboard", "/approval-inbox"]:
        response = client.get(path)
        assert "Sign in with GitHub" in response.text
        assert 'id="token"' not in response.text
        assert "unsafe-inline" not in response.headers["content-security-policy"]
    for name in ["auth.js", "approval_inbox.js", "approval_inbox.css"]:
        response = client.get("/auth/assets/" + name)
        assert response.status_code == 200
        assert "localStorage" not in response.text and "sessionStorage" not in response.text
    assert client.get("/auth/assets/settings.py").status_code == 404


def test_admin_mutations_require_csrf(environment):
    e = environment
    e.provider.user_id = 12
    login(e)
    assert e.client.put("/auth/users/12/access", json={"enabled": False}).status_code == 403
    policy = {
        "schema_version": "1",
        "enabled": False,
        "enabled_branches": [],
        "token_budget": 1000,
        "cost_budget_usd_micros": 1000,
        "verification_profile": "default",
    }
    assert (
        e.client.put(f"/api/repositories/{e.visible.repository}/policy", json=policy).status_code
        == 403
    )
