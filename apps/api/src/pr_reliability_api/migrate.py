"""Apply repository migrations before starting deployment services."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
from psycopg import Connection

from .db import apply_migrations


def run_migrations(
    database_url: str,
    migrations_path: Path,
    *,
    connect: Callable[[str], Connection[Any]] = psycopg.connect,
) -> tuple[str, ...]:
    """Apply tracked migrations with the existing checksum and lock boundary."""
    with connect(database_url) as connection:
        return apply_migrations(connection, migrations_path)


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    migrations_path = Path(os.environ.get("MIGRATIONS_PATH", "/app/migrations"))
    run_migrations(database_url, migrations_path)


if __name__ == "__main__":
    main()
