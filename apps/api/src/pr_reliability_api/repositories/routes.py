"""Private repository inventory and complete policy replacement."""

from fastapi import APIRouter, Header, HTTPException, Query
from pr_reliability_contracts.repositories import RepositoryPolicy
from psycopg.rows import dict_row

from ..identifiers import new_ulid
from ..reviewer import authorize_reviewer
from .store import audit, lock_installation


def create_repository_router(settings, webhook_settings, connection_factory) -> APIRouter:
    router = APIRouter()

    @router.get("/api/repositories")
    def inventory(
        authorization: str | None = Header(default=None),
        after: str = Query(
            default="", max_length=26, pattern=r"^(?:[0-7][0-9A-HJKMNP-TV-Z]{25})?$"
        ),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        authorize_reviewer(authorization, settings.reviewer_token)
        with connection_factory() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """SELECT r.public_id, r.github_repository_id, r.full_name, r.default_branch,
                          r.access_state, r.enabled, r.enabled_branches, r.token_budget,
                          r.cost_budget_usd_micros, r.verification_profile, r.last_sync_at,
                          r.last_webhook_at, r.last_review_run_at, i.state AS installation_state
                   FROM repositories r LEFT JOIN github_installations i
                     ON i.owner_id = r.owner_id AND i.installation_id = r.installation_id
                   WHERE r.owner_id = %s AND r.public_id > %s
                   ORDER BY r.public_id LIMIT %s""",
                (settings.owner_id, after, limit + 1),
            )
            rows = cursor.fetchall()
        return {
            "schema_version": "1",
            "repositories": rows[:limit],
            "next_cursor": rows[limit - 1]["public_id"] if len(rows) > limit else None,
        }

    @router.put("/api/repositories/{repository_id}/policy")
    def replace_policy(
        repository_id: str,
        policy: RepositoryPolicy,
        authorization: str | None = Header(default=None),
    ):
        authorize_reviewer(authorization, settings.reviewer_token)
        with connection_factory() as connection, connection.transaction():
            lock_installation(connection, settings.owner_id, webhook_settings.installation_id)
            row = connection.execute(
                """SELECT id, enabled, enabled_branches, token_budget, cost_budget_usd_micros,
                          verification_profile FROM repositories
                   WHERE owner_id = %s AND public_id = %s AND installation_id = %s FOR UPDATE""",
                (settings.owner_id, repository_id, webhook_settings.installation_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "repository not found")
            values = (
                policy.enabled,
                policy.enabled_branches,
                policy.token_budget,
                policy.cost_budget_usd_micros,
                policy.verification_profile,
            )
            if tuple(row[1:]) != values:
                connection.execute(
                    """UPDATE repositories SET enabled = %s, enabled_branches = %s,
                           token_budget = %s, cost_budget_usd_micros = %s,
                           verification_profile = %s, updated_at = now()
                       WHERE owner_id = %s AND id = %s""",
                    (*values, settings.owner_id, row[0]),
                )
                audit(
                    connection,
                    settings.owner_id,
                    webhook_settings.installation_id,
                    f"policy:{new_ulid()}",
                    "repository.policy_changed",
                    {
                        "repository_id": repository_id,
                        "actor_id": settings.actor_id,
                        "policy": policy.model_dump(),
                    },
                )
        return policy

    return router
