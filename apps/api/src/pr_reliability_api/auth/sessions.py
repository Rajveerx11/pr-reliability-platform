"""Durable, revocable opaque sessions and request authorization."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from psycopg.rows import dict_row

from ..identifiers import new_ulid

SESSION_COOKIE = "__Host-pr-session"
LOGIN_COOKIE = "__Host-pr-login"
LOGIN_RETURN_COOKIE = "__Host-pr-login-run"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    owner_id: str
    actor_id: str
    github_user_id: int | None
    login: str
    role: str
    repository_ids: list[int] | None


class Sessions:
    def __init__(self, settings, connection_factory, provider, *, now=None):
        self.settings = settings
        self.connection_factory = connection_factory
        self.provider = provider
        self.now = now or (lambda: datetime.now(UTC))
        self.cipher = Fernet(settings.session_key.encode())

    def begin(self, client_host):
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        client_hash = hmac.new(
            self.settings.session_key.encode(), client_host.encode(), hashlib.sha256
        ).hexdigest()
        with self.connection_factory() as connection:
            # Serialize admission and rate accounting across every API process.
            connection.execute("SELECT pg_advisory_xact_lock(440006)")
            connection.execute(
                "DELETE FROM github_login_attempts WHERE expires_at <= %s", (self.now(),)
            )
            connection.execute("DELETE FROM browser_sessions WHERE expires_at <= %s", (self.now(),))
            connection.execute(
                "DELETE FROM github_login_limits WHERE expires_at <= %s", (self.now(),)
            )
            attempts = connection.execute(
                """INSERT INTO github_login_limits VALUES (%s, 1, %s)
                   ON CONFLICT (client_hash) DO UPDATE
                   SET attempts = github_login_limits.attempts + 1 RETURNING attempts""",
                (client_hash, self.now() + timedelta(minutes=5)),
            ).fetchone()[0]
            if attempts > 20:
                raise HTTPException(
                    429, "Too many login attempts from this client", headers={"Retry-After": "300"}
                )
            count = connection.execute("SELECT count(*) FROM github_login_attempts").fetchone()[0]
            if count >= 1000:
                raise HTTPException(429, "Too many pending logins; try again later")
            connection.execute(
                """INSERT INTO github_login_attempts VALUES (%s, %s, %s, %s, %s)""",
                (
                    digest(state),
                    self.settings.owner_id,
                    digest(browser),
                    verifier,
                    self.now() + timedelta(minutes=5),
                ),
            )
        return state, browser, verifier

    def finish(self, state, browser, code, previous_session):
        with self.connection_factory() as connection:
            row = connection.execute(
                """DELETE FROM github_login_attempts
                   WHERE state_hash = %s AND owner_id = %s AND browser_hash = %s
                     AND expires_at > %s RETURNING verifier""",
                (digest(state), self.settings.owner_id, digest(browser), self.now()),
            ).fetchone()
        if row is None:
            raise HTTPException(403, "Login expired or state is invalid")
        token = self.provider.exchange(code, row[0])
        access = self.provider.access(token.value)
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection_factory() as connection:
            user = connection.execute(
                """INSERT INTO github_users (owner_id, github_user_id, actor_id, login)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (owner_id, github_user_id)
                   DO UPDATE SET login = excluded.login RETURNING enabled""",
                (self.settings.owner_id, access.user_id, new_ulid(), access.login),
            ).fetchone()
            if not user[0]:
                raise HTTPException(403, "User access revoked")
            connection.execute(
                "DELETE FROM browser_sessions WHERE token_hash = %s AND owner_id = %s",
                (digest(previous_session), self.settings.owner_id),
            )
            connection.execute(
                """INSERT INTO browser_sessions VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    digest(raw),
                    self.settings.owner_id,
                    access.user_id,
                    self.cipher.encrypt(token.value.encode()).decode(),
                    csrf,
                    self.now() + timedelta(seconds=token.expires_in),
                ),
            )
        return raw, token.expires_in

    def session(self, request):
        raw = request.cookies.get(SESSION_COOKIE, "")
        if not raw or len(raw) > 100:
            raise HTTPException(401, "Sign in with GitHub")
        with (
            self.connection_factory() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                """SELECT s.*, u.actor_id, u.login FROM browser_sessions s
                   JOIN github_users u USING (owner_id, github_user_id)
                   WHERE s.token_hash = %s AND s.owner_id = %s AND u.enabled
                     AND s.expires_at > %s""",
                (digest(raw), self.settings.owner_id, self.now()),
            )
            row = cursor.fetchone()
        if row is None:
            raise HTTPException(401, "Session expired or revoked; sign in with GitHub")
        return row

    def authorize(self, request, *, admin=False):
        row = self.session(request)
        role = self.settings.role(row["github_user_id"])
        if role is None or (admin and role != "admin"):
            raise HTTPException(
                403, "Administrator access required" if admin else "User not allowed"
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.csrf(request, row)
        try:
            token = self.cipher.decrypt(row["encrypted_token"].encode()).decode()
        except (InvalidToken, ValueError):
            raise HTTPException(401, "Session key changed; sign in with GitHub") from None
        access = self.provider.access(token)
        if access.user_id != row["github_user_id"]:
            raise HTTPException(403, "GitHub identity changed")
        self.session(request)  # Recheck revocation after the provider round trip.
        with self.connection_factory() as connection:
            repos = connection.execute(
                """SELECT r.id FROM repositories r JOIN github_installations i
                     ON i.owner_id = r.owner_id AND i.installation_id = r.installation_id
                   WHERE r.owner_id = %s AND r.installation_id = %s
                     AND r.github_repository_id = ANY(%s) AND r.access_state = 'active'
                     AND i.state = 'active' AND i.last_sync_at > %s""",
                (
                    self.settings.owner_id,
                    self.settings.installation_id,
                    list(access.repository_ids),
                    self.now() - timedelta(minutes=15),
                ),
            ).fetchall()
        return Principal(
            self.settings.owner_id,
            row["actor_id"],
            access.user_id,
            access.login,
            role,
            [item[0] for item in repos],
        )

    def csrf(self, request, row):
        supplied = request.headers.get("X-CSRF-Token", "")
        if request.headers.get("Origin") != self.settings.origin or not hmac.compare_digest(
            supplied.encode(), row["csrf_token"].encode()
        ):
            raise HTTPException(403, "Invalid request origin or CSRF token")

    def logout(self, request):
        row = self.session(request)
        self.csrf(request, row)
        with self.connection_factory() as connection:
            connection.execute(
                "DELETE FROM browser_sessions WHERE token_hash = %s", (row["token_hash"],)
            )

    def rotate(self, request):
        row = self.session(request)
        self.csrf(request, row)
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection_factory() as connection:
            changed = connection.execute(
                """UPDATE browser_sessions SET token_hash = %s, csrf_token = %s
                   WHERE token_hash = %s AND owner_id = %s AND expires_at > %s RETURNING 1""",
                (digest(raw), csrf, row["token_hash"], self.settings.owner_id, self.now()),
            ).fetchone()
        if not changed:
            raise HTTPException(401, "Session changed; reload the page")
        return raw, max(1, int((row["expires_at"] - self.now()).total_seconds()))

    def revoke(self, principal, user_id, enabled):
        with self.connection_factory() as connection:
            # Concurrent cross-revocations must not remove the final administrator.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"user-access:{self.settings.owner_id}",),
            )
            actor = connection.execute(
                """SELECT enabled FROM github_users WHERE owner_id = %s AND github_user_id = %s""",
                (self.settings.owner_id, principal.github_user_id),
            ).fetchone()
            if actor is None or not actor[0]:
                raise HTTPException(403, "Administrator access revoked")
            if not enabled and user_id in self.settings.admin_ids:
                remaining = connection.execute(
                    """SELECT 1 FROM github_users WHERE owner_id = %s AND enabled
                         AND github_user_id = ANY(%s) AND github_user_id <> %s LIMIT 1""",
                    (self.settings.owner_id, list(self.settings.admin_ids), user_id),
                ).fetchone()
                if remaining is None:
                    raise HTTPException(409, "At least one enabled administrator must remain")
            row = connection.execute(
                """UPDATE github_users SET enabled = %s WHERE owner_id = %s
                   AND github_user_id = %s RETURNING actor_id""",
                (enabled, self.settings.owner_id, user_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "User not found")
            if not enabled:
                connection.execute(
                    "DELETE FROM browser_sessions WHERE owner_id = %s AND github_user_id = %s",
                    (self.settings.owner_id, user_id),
                )
            from ..repositories.store import audit

            audit(
                connection,
                self.settings.owner_id,
                self.settings.installation_id,
                f"user-access:{new_ulid()}",
                "user.access_changed",
                {
                    "actor_id": principal.actor_id,
                    "github_user_id": principal.github_user_id,
                    "target_github_user_id": user_id,
                    "enabled": enabled,
                },
            )
