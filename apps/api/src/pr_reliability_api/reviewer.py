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
