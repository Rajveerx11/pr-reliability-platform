"""Same-origin GitHub login, session rotation, logout, and administrator revocation."""

import base64
import hashlib
import re
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from .sessions import LOGIN_COOKIE, LOGIN_RETURN_COOKIE, SESSION_COOKIE

_RUN_ID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class UserAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


def cookie(response, name, value, age):
    response.set_cookie(
        name, value, max_age=age, path="/", secure=True, httponly=True, samesite="lax"
    )


def create_login_router(sessions):
    router = APIRouter()

    @router.get("/auth/login")
    def login(
        request: Request,
        run: str | None = Query(default=None, max_length=26, pattern=_RUN_ID.pattern),
    ):
        # Use the ASGI peer, never parse caller-supplied forwarding headers here.
        state, browser, verifier = sessions.begin(
            request.client.host if request.client else "unknown"
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        query = urlencode(
            {
                "client_id": sessions.settings.client_id,
                "redirect_uri": sessions.settings.callback,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "allow_signup": "false",
            }
        )
        response = RedirectResponse(
            "https://github.com/login/oauth/authorize?" + query, status_code=303, headers=HEADERS
        )
        cookie(response, LOGIN_COOKIE, browser, 300)
        cookie(response, LOGIN_RETURN_COOKIE, run or "", 300)
        return response

    @router.get("/auth/callback", include_in_schema=False)
    def callback(
        request: Request,
        state: str = Query(default="", max_length=100),
        code: str = Query(default="", max_length=1024),
    ):
        run = request.cookies.get(LOGIN_RETURN_COOKIE, "")
        run_query = f"run={run}" if _RUN_ID.fullmatch(run) else ""
        try:
            if not state or not code:
                raise HTTPException(403, "GitHub login cancelled or invalid")
            raw, age = sessions.finish(
                state,
                request.cookies.get(LOGIN_COOKIE, ""),
                code,
                request.cookies.get(SESSION_COOKIE, ""),
            )
            target = f"/dashboard?{run_query}" if run_query else "/dashboard"
            response = RedirectResponse(target, status_code=303, headers=HEADERS)
            cookie(response, SESSION_COOKIE, raw, age)
        except HTTPException:
            # Remove OAuth callback parameters from the address bar even on failure.
            query = "&".join(part for part in (run_query, "login=failed") if part)
            response = RedirectResponse(f"/dashboard?{query}", status_code=303, headers=HEADERS)
        cookie(response, LOGIN_COOKIE, "", 0)
        cookie(response, LOGIN_RETURN_COOKIE, "", 0)
        return response

    @router.get("/auth/session")
    def session(request: Request):
        principal = sessions.authorize(request)
        row = sessions.session(request)
        return JSONResponse(
            {
                "login": principal.login,
                "github_user_id": principal.github_user_id,
                "role": principal.role,
                "csrf_token": row["csrf_token"],
                "expires_at": row["expires_at"].isoformat(),
            },
            headers=HEADERS,
        )

    @router.get("/auth/logout-token")
    def logout_token(request: Request):
        # This discloses no identity or repository data and grants no new authority.
        row = sessions.session(request)
        return JSONResponse({"csrf_token": row["csrf_token"]}, headers=HEADERS)

    @router.post("/auth/logout")
    def logout(request: Request):
        # Logout must work even during a GitHub outage or after upstream access removal.
        sessions.logout(request)
        response = Response(status_code=204, headers=HEADERS)
        cookie(response, SESSION_COOKIE, "", 0)
        return response

    @router.post("/auth/session/rotate")
    def rotate(request: Request):
        sessions.authorize(request)
        raw, age = sessions.rotate(request)
        response = Response(status_code=204, headers=HEADERS)
        cookie(response, SESSION_COOKIE, raw, age)
        return response

    @router.put("/auth/users/{github_user_id}/access")
    def access(github_user_id: int, body: UserAccess, request: Request):
        principal = sessions.authorize(request, admin=True)
        sessions.revoke(principal, github_user_id, body.enabled)
        return Response(status_code=204, headers=HEADERS)

    @router.get("/auth/assets/{name}", include_in_schema=False)
    def asset(name: str):
        from ..dashboard.routes import _SECURITY_HEADERS, _web_asset

        if name not in {"auth.js", "approval_inbox.js", "approval_inbox.css"}:
            raise HTTPException(404, "Asset not found")
        return Response(
            _web_asset(name).read_text(encoding="utf-8"),
            media_type="text/css" if name.endswith(".css") else "text/javascript",
            headers=_SECURITY_HEADERS,
        )

    return router
