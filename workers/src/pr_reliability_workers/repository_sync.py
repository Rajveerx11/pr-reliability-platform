"""Initial and periodic reconciliation for the configured private installation."""

import argparse
import asyncio
import logging
from pathlib import Path

import psycopg
from pr_reliability_api.repositories.store import apply_snapshot, lock_installation

from .providers.factory import _positive_int, _private_key, _required
from .providers.github_app import GitHubAppInstallationTokenProvider

_LOGGER = logging.getLogger(__name__)


async def reconcile_once(connection_factory, owner_id, installation_id, provider) -> bool:
    with connection_factory() as connection, connection.transaction():
        lock_installation(connection, owner_id, installation_id)
        revision = connection.execute(
            """SELECT revision FROM github_installations
               WHERE owner_id = %s AND installation_id = %s""",
            (owner_id, installation_id),
        ).fetchone()[0]
    # No database transaction stays open during network calls. CAS fences concurrent changes.
    async with asyncio.timeout(120):
        snapshot = await provider.inventory()
    if snapshot.installation_id != installation_id:
        raise ValueError("inventory installation is not authorized")
    with connection_factory() as connection, connection.transaction():
        return apply_snapshot(connection, owner_id, snapshot, revision)


async def run_sync(connection_factory, owner_id, installation_id, provider, *, once=False):
    while True:
        try:
            applied = await reconcile_once(connection_factory, owner_id, installation_id, provider)
            if once and not applied:
                raise RuntimeError("inventory changed during sync; retry required")
        except Exception:  # noqa: BLE001 -- keep daemon alive without logging provider details
            if once:
                raise RuntimeError("repository synchronization failed") from None
            _LOGGER.warning("repository synchronization failed; retrying")
        if once:
            return
        await asyncio.sleep(60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    database_url = _required("DATABASE_URL")
    owner_id = _required("OWNER_ID")
    installation_id = _positive_int("GITHUB_INSTALLATION_ID")
    provider = GitHubAppInstallationTokenProvider(
        _positive_int("GITHUB_APP_ID"),
        installation_id,
        _private_key(Path(_required("GITHUB_PRIVATE_KEY_PATH"))),
    )
    asyncio.run(
        run_sync(
            lambda: psycopg.connect(database_url, connect_timeout=10),
            owner_id,
            installation_id,
            provider,
            once=args.once,
        )
    )


if __name__ == "__main__":
    main()
