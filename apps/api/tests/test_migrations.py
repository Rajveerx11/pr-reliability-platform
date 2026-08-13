"""Tests for PostgreSQL migration discovery and execution."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from pr_reliability_api.db import (
    MigrationChangedError,
    MigrationHistoryError,
    apply_migrations,
    load_migrations,
)
from psycopg import Connection
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    ObjectNotInPrerequisiteState,
    UniqueViolation,
)

OWNER_ID = "01J00000000000000000000001"
OTHER_OWNER_ID = "01J00000000000000000000002"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"
OWNED_TABLES = (
    "repositories",
    "pull_requests",
    "runs",
    "findings",
    "approvals",
    "external_actions",
    "run_events",
)


def _public_id(sequence: int) -> str:
    return f"01J{sequence:023d}"


@pytest.fixture
def database() -> Iterator[Connection[tuple[object, ...]]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        if os.environ.get("CI"):
            pytest.fail("CI must provide TEST_DATABASE_URL")
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")

    schema = f"test_{uuid4().hex}"
    with psycopg.connect(database_url) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        connection.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        try:
            yield connection
        finally:
            connection.rollback()
            connection.execute("SET search_path TO public")
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
            connection.commit()


@pytest.fixture
def migrated_database(
    database: Connection[tuple[object, ...]],
) -> Connection[tuple[object, ...]]:
    expected = tuple(migration.version for migration in load_migrations())
    assert apply_migrations(database) == expected
    assert apply_migrations(database) == ()
    return database


def _seed_run(connection: Connection[tuple[object, ...]]) -> tuple[int, int, int]:
    repository_id = connection.execute(
        """
        INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (_public_id(1), OWNER_ID, 101, "owner/repository"),
    ).fetchone()[0]
    pull_request_id = connection.execute(
        """
        INSERT INTO pull_requests (
            public_id, owner_id, repository_id, github_number, base_sha, head_sha
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (_public_id(2), OWNER_ID, repository_id, 7, BASE_SHA, HEAD_SHA),
    ).fetchone()[0]
    run_id = connection.execute(
        """
        INSERT INTO runs (
            public_id, owner_id, pull_request_id, base_sha, head_sha,
            token_budget, cost_budget_usd_micros
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (_public_id(3), OWNER_ID, pull_request_id, BASE_SHA, HEAD_SHA, 10_000, 500_000),
    ).fetchone()[0]
    return int(repository_id), int(pull_request_id), int(run_id)


def test_loads_numbered_migrations_with_stable_checksum() -> None:
    migrations = load_migrations()

    assert [migration.version for migration in migrations] == sorted(
        migration.version for migration in migrations
    )
    assert [migration.version for migration in migrations][:2] == ["0001", "0002"]
    assert [migration.version for migration in migrations][-1] == "0003"
    assert all(re.fullmatch(r"[0-9a-f]{64}", migration.checksum) for migration in migrations)


def test_creates_owned_tables_with_public_and_internal_ids(
    migrated_database: Connection[tuple[object, ...]],
) -> None:
    columns = migrated_database.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable, is_identity
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = ANY(%s)
          AND column_name IN ('id', 'public_id', 'owner_id')
        """,
        (list(OWNED_TABLES),),
    ).fetchall()
    by_table = {
        table: {
            column: (data_type, nullable, identity)
            for table_name, column, data_type, nullable, identity in columns
            if table_name == table
        }
        for table in OWNED_TABLES
    }

    for table in OWNED_TABLES:
        assert by_table[table]["id"] == ("bigint", "NO", "YES")
        assert by_table[table]["public_id"] == ("character varying", "NO", "NO")
        assert by_table[table]["owner_id"] == ("character varying", "NO", "NO")


def test_blocks_cross_owner_parent_links(
    migrated_database: Connection[tuple[object, ...]],
) -> None:
    repository_id = migrated_database.execute(
        """
        INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (_public_id(10), OWNER_ID, 202, "owner/repository"),
    ).fetchone()[0]
    migrated_database.commit()

    with pytest.raises(ForeignKeyViolation), migrated_database.transaction():
        migrated_database.execute(
            """
                INSERT INTO pull_requests (
                    public_id, owner_id, repository_id, github_number, base_sha, head_sha
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
            (_public_id(11), OTHER_OWNER_ID, repository_id, 8, BASE_SHA, HEAD_SHA),
        )


def test_requires_valid_ulid_public_ids(
    migrated_database: Connection[tuple[object, ...]],
) -> None:
    with pytest.raises(CheckViolation), migrated_database.transaction():
        migrated_database.execute(
            """
            INSERT INTO repositories (public_id, owner_id, github_repository_id, full_name)
            VALUES (%s, %s, %s, %s)
            """,
            ("not-a-ulid", OWNER_ID, 203, "owner/repository"),
        )


def test_deduplicates_runs_approvals_and_events(
    migrated_database: Connection[tuple[object, ...]],
) -> None:
    _, pull_request_id, run_id = _seed_run(migrated_database)
    finding_id = migrated_database.execute(
        """
        INSERT INTO findings (
            public_id, owner_id, run_id, finding_key, category, severity,
            claim, confidence, evidence
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            _public_id(4),
            OWNER_ID,
            run_id,
            "finding-1",
            "correctness",
            "high",
            "claim",
            0.9,
            '[{"kind":"source_location"}]',
        ),
    ).fetchone()[0]
    with pytest.raises(ForeignKeyViolation), migrated_database.transaction():
        migrated_database.execute(
            """
            INSERT INTO approvals (
                public_id, owner_id, run_id, finding_id, actor_id, decision,
                head_sha, decided_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                _public_id(10),
                OWNER_ID,
                run_id,
                finding_id,
                _public_id(92),
                "approved",
                "c" * 40,
            ),
        )
    migrated_database.execute(
        """
        INSERT INTO approvals (
            public_id, owner_id, run_id, finding_id, actor_id, decision, head_sha, decided_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        """,
        (_public_id(5), OWNER_ID, run_id, finding_id, _public_id(90), "approved", HEAD_SHA),
    )
    migrated_database.execute(
        """
        INSERT INTO run_events (
            public_id, owner_id, run_id, event_key, event_type, occurred_at
        )
        VALUES (%s, %s, %s, %s, %s, now())
        """,
        (_public_id(6), OWNER_ID, run_id, "delivery-1", "run.started"),
    )
    migrated_database.execute(
        """
        INSERT INTO external_actions (
            public_id, owner_id, run_id, action_type, target_sha, idempotency_key
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (_public_id(12), OWNER_ID, run_id, "github_comment", HEAD_SHA, "publish-comment-1"),
    )
    migrated_database.commit()

    duplicate_statements = (
        (
            """
            INSERT INTO runs (
                public_id, owner_id, pull_request_id, base_sha, head_sha,
                token_budget, cost_budget_usd_micros
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (_public_id(7), OWNER_ID, pull_request_id, BASE_SHA, HEAD_SHA, 10_000, 500_000),
        ),
        (
            """
            INSERT INTO approvals (
                public_id, owner_id, run_id, finding_id, actor_id, decision,
                head_sha, decided_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            """,
            (_public_id(8), OWNER_ID, run_id, finding_id, _public_id(91), "rejected", HEAD_SHA),
        ),
        (
            """
            INSERT INTO external_actions (
                public_id, owner_id, run_id, action_type, target_sha, idempotency_key
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                _public_id(13),
                OWNER_ID,
                run_id,
                "github_comment",
                HEAD_SHA,
                "publish-comment-2",
            ),
        ),
        (
            """
            INSERT INTO run_events (
                public_id, owner_id, run_id, event_key, event_type, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, now())
            """,
            (_public_id(9), OWNER_ID, run_id, "delivery-1", "run.started"),
        ),
    )

    for statement, parameters in duplicate_statements:
        with pytest.raises(UniqueViolation), migrated_database.transaction():
            migrated_database.execute(statement, parameters)


def test_run_events_are_append_only(
    migrated_database: Connection[tuple[object, ...]],
) -> None:
    _, _, run_id = _seed_run(migrated_database)
    event_id = migrated_database.execute(
        """
        INSERT INTO run_events (
            public_id, owner_id, run_id, event_key, event_type, occurred_at
        )
        VALUES (%s, %s, %s, %s, %s, now())
        RETURNING id
        """,
        (_public_id(20), OWNER_ID, run_id, "event-1", "run.started"),
    ).fetchone()[0]
    migrated_database.commit()

    statements = (
        "UPDATE run_events SET event_type = 'changed' WHERE id = %s",
        "DELETE FROM run_events WHERE id = %s",
    )
    for statement in statements:
        with pytest.raises(ObjectNotInPrerequisiteState), migrated_database.transaction():
            migrated_database.execute(statement, (event_id,))

    with pytest.raises(ObjectNotInPrerequisiteState), migrated_database.transaction():
        migrated_database.execute("TRUNCATE run_events")


def test_rejects_an_applied_migration_that_changed(
    migrated_database: Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    copied_migration = tmp_path / "0001_initial.sql"
    for migration in MIGRATIONS.glob("*.sql"):
        shutil.copy(migration, tmp_path / migration.name)
    copied_migration.write_text(
        copied_migration.read_text(encoding="utf-8") + "\nSELECT 1;\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationChangedError, match="0001"):
        apply_migrations(migrated_database, tmp_path)


def test_rejects_missing_applied_migration_history(
    migrated_database: Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    (tmp_path / "0002_next.sql").write_text("SELECT 1;\n", encoding="utf-8")

    with pytest.raises(MigrationHistoryError, match="0001"):
        apply_migrations(migrated_database, tmp_path)


def test_generation_migration_updates_existing_command_events(
    database: Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    for name in ("0001_initial.sql", "0002_github_webhook_deliveries.sql"):
        shutil.copy(MIGRATIONS / name, tmp_path / name)
    assert apply_migrations(database, tmp_path) == ("0001", "0002")
    _, pull_request_id, first_run_id = _seed_run(database)
    second_run_id = database.execute(
        """
        INSERT INTO runs (
            public_id, owner_id, pull_request_id, base_sha, head_sha,
            token_budget, cost_budget_usd_micros
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (_public_id(31), OWNER_ID, pull_request_id, BASE_SHA, "c" * 40, 10_000, 500_000),
    ).fetchone()[0]
    for sequence, run_id in enumerate((first_run_id, second_run_id), start=40):
        database.execute(
            """
            INSERT INTO run_events (
                public_id, owner_id, run_id, event_key, event_type, event_data, occurred_at
            ) VALUES (%s, %s, %s, %s, 'run.command_created', %s::jsonb, now())
            """,
            (
                _public_id(sequence),
                OWNER_ID,
                run_id,
                f"command-{sequence}",
                '{"schema_version":"1"}',
            ),
        )
    database.commit()
    shutil.copy(MIGRATIONS / "0003_order_run_generations.sql", tmp_path)

    assert apply_migrations(database, tmp_path) == ("0003",)
    generations = database.execute("SELECT generation FROM runs ORDER BY id").fetchall()
    event_generations = database.execute(
        "SELECT (event_data ->> 'generation')::integer FROM run_events ORDER BY run_id"
    ).fetchall()

    assert generations == [(1,), (2,)]
    assert event_generations == [(1,), (2,)]
