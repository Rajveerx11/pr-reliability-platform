"""Validated single-owner GitHub App browser configuration."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class LoginSettings:
    owner_id: str
    installation_id: int
    account_id: int
    client_id: str
    client_secret: str = field(repr=False)
    session_key: str = field(repr=False)
    origin: str
    reviewer_ids: frozenset[int]
    admin_ids: frozenset[int]

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("GITHUB_LOGIN_ORIGIN must be an HTTPS origin without a path")
        if not self.owner_id or min(self.installation_id, self.account_id) <= 0:
            raise ValueError("login owner and GitHub IDs are required")
        if not self.client_id.strip() or not self.client_secret.strip():
            raise ValueError("GitHub client credentials are required")
        if not self.admin_ids or any(i <= 0 for i in self.admin_ids | self.reviewer_ids):
            raise ValueError("positive GitHub user IDs and at least one administrator are required")
        Fernet(self.session_key.encode())

    @property
    def callback(self):
        return self.origin + "/auth/callback"

    def role(self, user_id: int) -> str | None:
        if user_id in self.admin_ids:
            return "admin"
        return "reviewer" if user_id in self.reviewer_ids else None


def from_environment(owner_id, installation_id, environment):
    def required(name):
        value = environment.get(name, "").strip()
        if not value:
            raise ValueError(f"{name} is required")
        return value

    def ids(name):
        raw = environment.get(name, "").strip()
        return frozenset(int(item.strip()) for item in raw.split(",")) if raw else frozenset()

    return LoginSettings(
        owner_id=owner_id,
        installation_id=installation_id,
        account_id=int(required("GITHUB_ALLOWED_ACCOUNT_ID")),
        client_id=required("GITHUB_OAUTH_CLIENT_ID"),
        client_secret=required("GITHUB_OAUTH_CLIENT_SECRET"),
        session_key=required("SESSION_ENCRYPTION_KEY"),
        origin=required("GITHUB_LOGIN_ORIGIN"),
        reviewer_ids=ids("GITHUB_REVIEWER_IDS"),
        admin_ids=ids("GITHUB_ADMIN_IDS"),
    )
