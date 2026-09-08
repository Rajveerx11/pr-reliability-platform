"""Shared reviewer authentication for private browser APIs."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, status


def authorize_reviewer(authorization: str | None, expected_token: str) -> None:
    """Require one exact bearer token without leaking comparison timing."""

    scheme, separator, token = (authorization or "").partition(" ")
    valid = (
        separator == " "
        and scheme.lower() == "bearer"
        and bool(token)
        and hmac.compare_digest(token, expected_token)
    )
    if not valid:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "reviewer authorization required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def request_reviewer(request, settings, sessions=None, *, admin=False):
    """Production sessions; explicit legacy settings remain an embedding/test dependency."""
    if sessions is not None:
        principal = sessions.authorize(request, admin=admin)
        if principal.owner_id != settings.owner_id:
            raise HTTPException(403, "Owner access denied")
        return principal
    authorize_reviewer(request.headers.get("Authorization"), settings.reviewer_token)
    from .auth.sessions import Principal

    return Principal(settings.owner_id, settings.actor_id, None, "test reviewer", "admin", None)
