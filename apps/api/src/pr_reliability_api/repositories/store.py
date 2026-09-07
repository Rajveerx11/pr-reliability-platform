"""Serialize installation changes and admit reviews from synchronized policy."""

import json

from pr_reliability_contracts.repositories import InstallationSnapshot, RepositoryPolicy

from ..identifiers import new_ulid


def lock_installation(connection, owner_id: str, installation_id: int) -> None:
    # Also serializes the first insert, before an installation row exists.
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"installation:{owner_id}:{installation_id}",),
    )
    connection.execute(
        """INSERT INTO github_installations (owner_id, installation_id) VALUES (%s, %s)
           ON CONFLICT DO NOTHING""",
        (owner_id, installation_id),
    )


def audit(connection, owner_id, installation_id, event_key, event_type, data) -> bool:
    return (
        connection.execute(
            """INSERT INTO repository_events
           (owner_id, installation_id, event_key, event_type, event_data)
           VALUES (%s, %s, %s, %s, %s::jsonb)
           ON CONFLICT (owner_id, event_key) DO NOTHING RETURNING id""",
            (owner_id, installation_id, event_key, event_type, json.dumps(data)),
        ).fetchone()
        is not None
    )


def apply_snapshot(connection, owner_id, snapshot: InstallationSnapshot, revision: int) -> bool:
    """Apply only a complete snapshot with no intervening webhook or reconciliation."""
    installation_id = snapshot.installation_id
    lock_installation(connection, owner_id, installation_id)
    updated = connection.execute(
        """UPDATE github_installations SET state = %s, revision = revision + 1,
               last_sync_at = now()
           WHERE owner_id = %s AND installation_id = %s AND revision = %s RETURNING revision""",
        (snapshot.state, owner_id, installation_id, revision),
    ).fetchone()
    if updated is None:
        return False
    repositories = snapshot.repositories if snapshot.state == "active" else []
    for repo in repositories:
        before = connection.execute(
            """SELECT full_name, default_branch, access_state, installation_id FROM repositories
               WHERE owner_id = %s AND github_repository_id = %s""",
            (owner_id, repo.id),
        ).fetchone()
        after = (repo.full_name, repo.default_branch, "active", installation_id)
        connection.execute(
            """INSERT INTO repositories
               (public_id, owner_id, github_repository_id, full_name, installation_id,
                access_state, default_branch, last_sync_at)
               VALUES (%s, %s, %s, %s, %s, 'active', %s, now())
               ON CONFLICT (owner_id, github_repository_id) DO UPDATE SET
                   full_name = EXCLUDED.full_name, installation_id = EXCLUDED.installation_id,
                   access_state = 'active', default_branch = EXCLUDED.default_branch,
                   last_sync_at = now(), updated_at = now()""",
            (new_ulid(), owner_id, repo.id, repo.full_name, installation_id, repo.default_branch),
        )
        if before != after:
            audit(
                connection,
                owner_id,
                installation_id,
                f"sync:{installation_id}:{updated[0]}:{repo.id}",
                "repository.synchronized",
                {
                    "github_repository_id": repo.id,
                    "before": _metadata(before),
                    "after": _metadata(after),
                },
            )
    removed = connection.execute(
        """UPDATE repositories SET access_state = 'removed', installation_id = %s, last_sync_at = now(),
               updated_at = now()
           WHERE owner_id = %s AND (installation_id = %s OR installation_id IS NULL)
             AND access_state <> 'removed'
             AND NOT (github_repository_id = ANY(%s::bigint[])) RETURNING github_repository_id""",
        (installation_id, owner_id, installation_id, [repo.id for repo in repositories]),
    ).fetchall()
    for (repository_id,) in removed:
        audit(
            connection,
            owner_id,
            installation_id,
            f"sync:{installation_id}:{updated[0]}:{repository_id}",
            "repository.removed",
            {"github_repository_id": repository_id},
        )
    audit(
        connection,
        owner_id,
        installation_id,
        f"sync:{installation_id}:{updated[0]}",
        "installation.reconciled",
        {"state": snapshot.state, "repository_count": len(repositories)},
    )
    return True


def _metadata(row):
    if row is None:
        return None
    return dict(zip(("full_name", "default_branch", "access_state", "installation_id"), row))


def admitted_policy(connection, owner_id, installation_id, github_repository_id, branch):
    row = connection.execute(
        """SELECT r.enabled, r.enabled_branches, r.token_budget,
                  r.cost_budget_usd_micros, r.verification_profile
           FROM repositories r JOIN github_installations i
             ON i.owner_id = r.owner_id AND i.installation_id = r.installation_id
           WHERE r.owner_id = %s AND r.installation_id = %s AND r.github_repository_id = %s
             AND r.access_state = 'active' AND i.state = 'active'
             AND i.last_sync_at > now() - interval '15 minutes'""",
        (owner_id, installation_id, github_repository_id),
    ).fetchone()
    if row is None or not row[0] or (row[1] and branch not in row[1]):
        return None
    return RepositoryPolicy(
        schema_version="1",
        enabled=row[0],
        enabled_branches=row[1],
        token_budget=row[2],
        cost_budget_usd_micros=row[3],
        verification_profile=row[4],
    )
