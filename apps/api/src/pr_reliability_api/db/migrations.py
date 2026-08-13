"""Apply immutable SQL migrations in version order."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import Connection

_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
_MIGRATION_LOCK_ID = 7_249_581_303_001
_SOURCE_DIRECTORY = Path(__file__).resolve().parents[5] / "migrations"
_PACKAGED_DIRECTORY = Path(__file__).resolve().parents[1] / "_migrations"
_DEFAULT_DIRECTORY = _PACKAGED_DIRECTORY if _PACKAGED_DIRECTORY.is_dir() else _SOURCE_DIRECTORY


class MigrationChangedError(RuntimeError):
    """An applied migration no longer matches its recorded content."""


class MigrationHistoryError(RuntimeError):
    """Migration files no longer form a safe continuation of database history."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable SQL migration loaded from disk."""

    version: str
    name: str
    sql: str
    checksum: str


def load_migrations(directory: Path = _DEFAULT_DIRECTORY) -> tuple[Migration, ...]:
    """Load valid migration files and reject duplicate versions."""

    migrations: list[Migration] = []
    versions: set[str] = set()

    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"invalid migration filename: {path.name}")

        version = match.group("version")
        if version in versions:
            raise ValueError(f"duplicate migration version: {version}")
        versions.add(version)

        raw = path.read_bytes()
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                sql=raw.decode("utf-8"),
                checksum=hashlib.sha256(raw).hexdigest(),
            )
        )

    if not migrations:
        raise ValueError(f"no migrations found in {directory}")
    return tuple(migrations)


def apply_migrations(
    connection: Connection[Any], directory: Path = _DEFAULT_DIRECTORY
) -> tuple[str, ...]:
    """Apply pending migrations once and verify already-applied checksums."""

    migrations = load_migrations(directory)
    applied_now: list[str] = []

    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version varchar(4) PRIMARY KEY,
                name text NOT NULL,
                checksum varchar(64) NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations"
        ).fetchall()
        applied = {str(version): (str(name), str(checksum)) for version, name, checksum in rows}
        available_versions = {migration.version for migration in migrations}
        missing_versions = sorted(set(applied) - available_versions)
        if missing_versions:
            raise MigrationHistoryError(
                f"applied migrations are missing from disk: {', '.join(missing_versions)}"
            )

        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded is not None:
                recorded_name, recorded_checksum = recorded
                if recorded_name != migration.name or recorded_checksum != migration.checksum:
                    raise MigrationChangedError(
                        f"applied migration {migration.version} has changed"
                    )
                continue

            later_versions = sorted(version for version in applied if version > migration.version)
            if later_versions:
                raise MigrationHistoryError(
                    f"migration {migration.version} cannot run after {later_versions[0]}"
                )

            connection.execute(migration.sql)
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name, checksum)
                VALUES (%s, %s, %s)
                """,
                (migration.version, migration.name, migration.checksum),
            )
            applied_now.append(migration.version)

    return tuple(applied_now)
