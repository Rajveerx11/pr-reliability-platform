# GitHub login and sessions

Issue [#44](https://github.com/Rajveerx11/pr-reliability-platform/issues/44) replaces the production
dashboard token with individual GitHub identities. The environment entrypoint accepts only
GitHub sessions. Explicit legacy bearer dependencies remain for existing embedded tests; there
is no production bearer fallback or environment switch.

## Configure the existing GitHub App

1. Enable the GitHub App's web authorization flow. Register the exact callback
   `https://YOUR_PRIVATE_HOST/auth/callback`. Use its OAuth client ID and client secret, not its
   numeric App ID or private key. Keep expiring user access tokens enabled.
2. Set `GITHUB_LOGIN_ORIGIN` to the private HTTPS origin, without a trailing slash. On the VM it
   must equal `PRIVATE_BASE_URL`. The browser must reach both GitHub and that private origin.
3. Set `GITHUB_ALLOWED_ACCOUNT_ID` to the stable numeric GitHub user or organization ID that owns
   the configured installation. Set `GITHUB_ADMIN_IDS` and optional `GITHUB_REVIEWER_IDS` to
   comma-separated numeric user IDs. At least one administrator is required. Organization
   installations still require an explicit person allowlist; membership alone grants no access.
4. Store `GITHUB_OAUTH_CLIENT_SECRET` and `SESSION_ENCRYPTION_KEY` in the external mode-0600
   environment file. The latter must be a new Fernet key: a URL-safe base64 encoding of 32 random
   bytes, generated with `cryptography.fernet.Fernet.generate_key()` during secret provisioning.
   Never use the shipped placeholders. Only the API receives these secrets.
5. Apply migration 0006, complete installation sync, and restart the API with the new configuration.
   Open `/dashboard`, select **Sign in with GitHub**, then inspect the displayed login and role.

The API now needs outbound HTTPS to `github.com` and `api.github.com`. It still receives no
GitHub App private key, model credential, or Docker socket. Follow GitHub's
[GitHub App user authorization flow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app).
The implementation uses state bound to a separate browser cookie and S256 PKCE. State expires
after five minutes and is consumed once. No access token or session bearer is placed in a URL.
GitHub's short-lived authorization code necessarily arrives on the callback query; access logs
are disabled in the shipped Uvicorn commands and callback responses immediately redirect to
a clean URL. Do not enable query-string logging in an upstream proxy.

## Authorization and storage

Every dashboard, approval, and repository API request validates the local session, configured
user role, live GitHub user identity, matching installation account, and live repositories
accessible to that user through the installation. Results are intersected with active, freshly
synchronized local repository access. Filtering happens before counts, pagination, detail reads,
and writes. Inaccessible details return 404. A GitHub access denial returns 403; an unavailable
provider returns 503. No stale provider allowlist is used as a fallback.

Reviewers may read their repositories and record approval decisions. Administrators may also
replace policy and revoke/re-enable individual users. Administrators still need repository access.
Pausing review admission does not remove an authorized user's history access.

The database stores stable GitHub IDs mapped to existing ULID actors. Approval and policy audit
events include both IDs. Provider access tokens are Fernet-encrypted with the API-only key;
session bearers are stored only as SHA-256 hashes. Backups therefore contain encrypted user
credentials and must remain protected. The browser receives an opaque `__Host-pr-session`
cookie with Secure, HttpOnly, SameSite=Lax, and Path=/, without Domain. It stores no credentials
in localStorage or sessionStorage. A CSRF token is returned only to same-origin JavaScript memory.

Sessions expire after the lesser of eight hours or the provider-reported token lifetime. Login
replaces that browser's previous session. Open pages rotate the session and CSRF token every
15 minutes without extending the absolute expiry. Refresh tokens are not retained; sign in again
after expiry. Rotation invalidates the old bearer immediately. Logout deletes the session.
Changing the encryption key requires everyone to sign in again and makes older encrypted tokens
unreadable. Keep the external key available for intended database recovery.

## Session API

| Method | Path | Behavior |
|---|---|---|
| GET | `/auth/login` | Begin browser-bound GitHub authorization |
| GET | `/auth/callback` | Consume authorization state and create a new session |
| GET | `/auth/session` | Check live access; return login, role, expiry, and CSRF token |
| GET | `/auth/logout-token` | Return only local-session CSRF proof so logout works during a GitHub outage |
| POST | `/auth/logout` | Delete this browser's session |
| POST | `/auth/session/rotate` | Replace the bearer and CSRF token, preserving absolute expiry |
| PUT | `/auth/users/{github_user_id}/access` | Administrator sets `{"enabled": false}` or `{"enabled": true}` |

All state-changing browser requests require the exact configured `Origin` and `X-CSRF-Token`.
Webhooks retain their separate HMAC authentication. To revoke a person, an administrator sends
the access request from the signed-in private origin. Revocation deletes all of that person's
sessions and blocks later login. Re-enabling permits a fresh login; deleted sessions never return.
The person's ID must also remain in the operator allowlist. There is no user-management UI yet.

## Local TLS and verification

The development Compose API port is an HTTP upstream, not a browser login origin. Put it behind
a trusted local TLS proxy, or use the private VM's existing Caddy boundary. For example, configure
Caddy with `https://localhost:8443`, `tls internal`, and `reverse_proxy 127.0.0.1:8000`; trust its
local CA and register `https://localhost:8443/auth/callback` on a test GitHub App. Use the same
origin in the environment. Never disable Secure cookies for development.

Automated tests use isolated PostgreSQL schemas and fake GitHub responses. Browser acceptance
uses a separate local TLS server and test identities; it cannot publish to GitHub. These tests
prove application boundaries, not acceptance of a real GitHub App or Linux rollout. Those remain
part of issue #15. On rollback, restore the previous approved application/configuration as one
unit; never expose its older shared-token access to production users.
