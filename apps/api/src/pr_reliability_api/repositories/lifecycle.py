"""Signed lifecycle deliveries revoke immediately; REST reconciliation grants access."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .store import audit, lock_installation


class _Installation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictInt = Field(gt=0)


class _Repository(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictInt = Field(gt=0)


class InstallationDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: Literal["created", "deleted", "suspend", "unsuspend", "new_permissions_accepted"]
    installation: _Installation


class RepositoryDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: Literal["added", "removed"]
    installation: _Installation
    repositories_added: list[_Repository] = Field(max_length=10_000)
    repositories_removed: list[_Repository] = Field(max_length=10_000)


def receive_lifecycle(connection, settings, event_type, delivery_id, payload):
    owner_id, installation_id = settings.owner_id, settings.installation_id
    lock_installation(connection, owner_id, installation_id)
    removed = (
        [repo.id for repo in payload.repositories_removed]
        if isinstance(payload, RepositoryDelivery)
        else []
    )
    if not audit(
        connection,
        owner_id,
        installation_id,
        f"webhook:{delivery_id}",
        f"{event_type}.{payload.action}",
        {
            "removed_repository_ids": removed,
            "added_repository_ids": [r.id for r in payload.repositories_added]
            if isinstance(payload, RepositoryDelivery)
            else [],
        },
    ):
        return {"accepted": True, "duplicate": True, "command_id": None}
    # Never use an old create/add/unsuspend delivery to grant access. It only requests a sync.
    state = {"deleted": "deleted", "suspend": "suspended"}.get(payload.action)
    connection.execute(
        """UPDATE github_installations SET revision = revision + 1,
               state = COALESCE(%s, state), last_webhook_at = now()
           WHERE owner_id = %s AND installation_id = %s""",
        (state, owner_id, installation_id),
    )
    if isinstance(payload, RepositoryDelivery):
        connection.execute(
            """UPDATE repositories SET last_webhook_at = now()
               WHERE owner_id = %s AND installation_id = %s
                 AND github_repository_id = ANY(%s::bigint[])""",
            (owner_id, installation_id, [r.id for r in payload.repositories_added]),
        )
    if removed or state is not None:
        connection.execute(
            """UPDATE repositories SET access_state = 'removed', last_webhook_at = now(),
                   updated_at = now()
               WHERE owner_id = %s AND installation_id = %s
                 AND (%s OR github_repository_id = ANY(%s::bigint[]))""",
            (owner_id, installation_id, state is not None, removed),
        )
    return {"accepted": True, "duplicate": False, "command_id": None}
