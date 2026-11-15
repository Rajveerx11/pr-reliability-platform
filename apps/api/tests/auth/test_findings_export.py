"""Approved exports use the same live session, owner and repository fences as the inbox."""

import json
from datetime import timedelta

import pytest

from .conftest import HEAD, csrf, decision, login, public_id

URL = "/api/findings/approved.jsonl"


def approve_seeded_findings(environment, visible_decision):
    with environment.database() as connection:
        for index, item in enumerate(
            [environment.visible, environment.hidden, environment.other], start=100
        ):
            value = visible_decision if item == environment.visible else "approved"
            if value is None:
                continue
            connection.execute(
                """INSERT INTO approvals
                   (public_id, owner_id, run_id, finding_id, actor_id, decision, head_sha, decided_at)
                   SELECT %s, f.owner_id, f.run_id, f.id, %s, %s, r.head_sha,
                          '2026-11-14T12:00:00Z'::timestamptz
                   FROM findings f JOIN runs r ON r.id = f.run_id AND r.owner_id = f.owner_id
                   WHERE f.public_id = %s""",
                (public_id(index), public_id(199), value, item.finding),
            )


@pytest.mark.parametrize("visible_decision", [None, "rejected", "approved"])
def test_export_only_approved_structured_fields_in_live_scope(environment, visible_decision):
    e = environment
    approve_seeded_findings(e, visible_decision)
    assert e.client.get(URL).status_code == 401
    login(e)
    response = e.client.get(URL)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    rows = [json.loads(line) for line in response.text.splitlines()]
    assert rows == (
        [
            {
                "repository": "owner/repo-91",
                "pr_number": 1,
                "head_sha": HEAD,
                "category": "correctness",
                "severity": "high",
                "approval_decision": "approved",
                "approved_at": "2026-11-14T12:00:00+00:00",
                "pr_outcome": None,
            }
        ]
        if visible_decision == "approved"
        else []
    )
    assert not rows or response.text.endswith("\n")
    with e.database() as connection:
        assert connection.execute("SELECT count(*) FROM external_actions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM run_events").fetchone()[0] == 0


def test_export_contains_only_human_decisions_and_keeps_original_head(environment):
    e = environment
    login(e)
    assert e.client.get(URL).text == ""
    response = e.client.post(
        f"/api/approval-inbox/{e.visible.finding}/decision", json=decision(), headers=csrf(e.client)
    )
    assert response.status_code == 200
    with e.database() as connection:
        connection.execute("UPDATE pull_requests SET head_sha = %s, state = 'closed'", ("c" * 40,))
        connection.execute("UPDATE runs SET state = 'published'")
    row = json.loads(e.client.get(URL).text)
    assert row["head_sha"] == HEAD
    assert row["approval_decision"] == "approved"
    assert row["pr_outcome"] is None  # Closed does not establish whether the PR merged.
    assert row["approved_at"] == response.json()["decided_at"].replace("Z", "+00:00")


@pytest.mark.parametrize("loss", ["repository", "owner", "expired", "revoked", "stale_sync"])
def test_export_denies_lost_access(environment, loss):
    e = environment
    approve_seeded_findings(e, "approved")
    login(e)
    assert len(e.client.get(URL).text.splitlines()) == 1
    if loss == "repository":
        e.provider.repositories = frozenset()
    elif loss == "expired":
        e.clock[0] += timedelta(days=1)
    else:
        with e.database() as connection:
            if loss == "owner":
                connection.execute(
                    """INSERT INTO github_users (owner_id, github_user_id, actor_id, login)
                       VALUES (%s, 11, %s, 'other-owner')""",
                    (public_id(999), public_id(998)),
                )
                connection.execute("UPDATE browser_sessions SET owner_id = %s", (public_id(999),))
            elif loss == "revoked":
                connection.execute("UPDATE github_users SET enabled = false")
            else:
                connection.execute(
                    "UPDATE github_installations SET last_sync_at = now() - interval '1 hour'"
                )
    response = e.client.get(URL)
    if loss in {"expired", "revoked", "owner"}:
        assert response.status_code == 401
    else:
        assert response.status_code == 200
        assert response.text == ""
