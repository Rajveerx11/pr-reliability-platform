"""Deployment migration entry point tests."""

from __future__ import annotations

from pathlib import Path

from pr_reliability_api import migrate


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def test_run_migrations_uses_supplied_database_and_tracked_directory(
    tmp_path: Path, monkeypatch
) -> None:
    connection = FakeConnection()
    called: list[tuple[object, Path]] = []
    monkeypatch.setattr(
        migrate,
        "apply_migrations",
        lambda received, path: called.append((received, path)) or ("0001",),
    )

    applied = migrate.run_migrations(
        "postgresql://database/app",
        tmp_path,
        connect=lambda database_url: connection if database_url.endswith("/app") else None,
    )

    assert applied == ("0001",)
    assert called == [(connection, tmp_path)]
