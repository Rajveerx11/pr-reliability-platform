"""Real browser sessions gate operations reads and CSRF-protected drain writes."""

from pr_reliability_api.operations.store import OperationsStore

from .conftest import OWNER, csrf, login


def test_operations_require_admin_and_drain_requires_csrf(environment):
    e = environment
    login(e)
    assert e.client.get("/api/operations/overview").status_code == 403
    assert e.client.get("/api/operations/metrics").status_code == 403
    assert (
        e.client.post("/api/operations/runners/review-1/drain", headers=csrf(e.client)).status_code
        == 403
    )
    e.client.post("/auth/logout", headers=csrf(e.client))
    e.provider.user_id = 12
    login(e)
    overview = e.client.get("/api/operations/overview")
    assert overview.status_code == 200
    assert overview.json()["awaiting_approval"] == 1  # live GitHub repo/owner scope
    assert e.client.post("/api/operations/runners/review-1/drain").status_code == 403
    invalid = csrf(e.client) | {"Origin": "https://attacker.test"}
    assert (
        e.client.post("/api/operations/runners/review-1/drain", headers=invalid).status_code == 403
    )
    assert (
        e.client.post("/api/operations/runners/review-1/drain", headers=csrf(e.client)).status_code
        == 404
    )
    e.provider.unavailable = True
    assert e.client.get("/api/operations/overview").status_code == 503
    assert (
        e.client.post("/api/operations/runners/review-1/drain", headers=invalid).status_code == 403
    )
    assert OperationsStore(e.database).snapshot(OWNER, "pr-review")["runners"] == []
