"""Only genuinely overlapping GitHub checks share work, never stale permissions."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pr_reliability_api.auth import github


@pytest.mark.parametrize("fails", [False, True])
def test_overlapping_access_shares_result_or_error_and_next_request_is_live(monkeypatch, fails):
    entered, release, joined = Event(), Event(), Event()
    calls = []

    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)

    monkeypatch.setattr(github, "Future", ObservedFuture)
    provider = github.GitHubIdentity(SimpleNamespace())

    def access(token):
        calls.append(token)
        entered.set()
        assert release.wait(5)
        if fails:
            raise HTTPException(503, "GitHub identity service unavailable")
        return github.Access(11, "reviewer", frozenset({91}) if len(calls) == 1 else frozenset())

    monkeypatch.setattr(provider, "_access", access)

    def request():
        try:
            return provider.access("private-token")
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request)
        assert entered.wait(5)
        second = pool.submit(request)
        assert joined.wait(5)
        release.set()
        a, b = first.result(), second.result()
    assert len(calls) == 1 and a == b
    assert provider._inflight == {}
    if fails:
        assert a == 503
        assert request() == 503
    else:
        assert a.repository_ids == frozenset({91})
        assert request().repository_ids == frozenset()
    assert len(calls) == 2


def test_different_provider_credentials_do_not_share_access(monkeypatch):
    provider = github.GitHubIdentity(SimpleNamespace())
    calls = []

    def access(token):
        calls.append(token)
        return github.Access(11 if token == "one" else 12, "user", frozenset())

    monkeypatch.setattr(provider, "_access", access)
    assert provider.access("one").user_id == 11
    assert provider.access("two").user_id == 12
    assert calls == ["one", "two"]
