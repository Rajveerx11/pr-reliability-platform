# Configuration reference

Baseline: `675257a`, verified 2026-09-07. Use [.env.example](../.env.example) for processes run on
the development host. Use [deployment.env.example](../infra/deployment/deployment.env.example)
for the private VM stack. Neither template is a production-ready configuration.

Python entrypoints read process environment. They do not automatically load `.env`.
`uv run --env-file .env ...` loads it for a host command. Compose `--env-file` provides variable
substitution; only values explicitly listed under a service's `environment` enter that container.

## Control plane and synchronization

| Variable | Consumers | Requirement or default |
|---|---|---|
| `DATABASE_URL` | API, migration, dispatcher, activities, sync | Required PostgreSQL URL |
| `OWNER_ID` | API, activities, sync | Required stable owner ULID |
| `GITHUB_OAUTH_CLIENT_ID` | API | GitHub App OAuth client ID |
| `GITHUB_OAUTH_CLIENT_SECRET` | API | External GitHub App OAuth client secret |
| `GITHUB_LOGIN_ORIGIN` | API | Exact private HTTPS origin, no trailing slash |
| `GITHUB_ALLOWED_ACCOUNT_ID` | API | Numeric user/organization ID owning the installation |
| `GITHUB_ADMIN_IDS` | API | Required comma-separated numeric administrator IDs |
| `GITHUB_REVIEWER_IDS` | API | Optional comma-separated numeric reviewer IDs |
| `SESSION_ENCRYPTION_KEY` | API | External Fernet key for persisted user access tokens |
| `GITHUB_INSTALLATION_ID` | API, activities, sync | Required positive installation ID |
| `GITHUB_WEBHOOK_SECRET` | API | Required HMAC secret |
| `GITHUB_APP_ID` | Activities, sync | Required positive GitHub App ID |
| `GITHUB_PRIVATE_KEY_PATH` | Activities, sync | Absolute path to readable RSA key file; never the key contents |
| `GITHUB_APP_BOT_USER_ID` | Activities | Numeric bot user ID used to verify publish retries |
| `HEALTH_CHECK_TIMEOUT_SECONDS` | API | Positive seconds; default `2` per readiness dependency |

See [GitHub login](authentication.md) for provisioning, sessions, revocation, and local TLS.
The production API no longer reads `APPROVAL_ACTOR_ID` or `APPROVAL_REVIEWER_TOKEN`.
The API receives no GitHub private key. Sync receives no model key or Docker socket.
Inventory runs every 60 seconds with a 120-second whole-sync timeout and a 15-minute freshness
limit. These are code constants, not environment settings. One owner/installation pair is configured
per private deployment. Changing that pair is not an automatic tenant migration.

## Workflow and provider activity

| Variable | Requirement or default |
|---|---|
| `TEMPORAL_ADDRESS` | Host/port; host-process default `localhost:7233` |
| `TEMPORAL_NAMESPACE` | Default `default` |
| `TEMPORAL_TASK_QUEUE` | Default `pr-review`; workflow, activity, and dispatcher must agree |
| `REVIEW_ACTIVITY_OPERATIONS_FACTORY` | `pr_reliability_workers.providers:create_operations` |
| `MODEL_PROVIDER` | Required `openai` for the built-in production factory |
| `OPENAI_API_KEY` | Required provider secret |
| `OPENAI_MODEL` | Required explicitly selected model; no automatic model choice |
| `OPENAI_MAX_OUTPUT_TOKENS` | Positive integer; default `4096` |
| `OPENAI_TIMEOUT_SECONDS` | Positive seconds; default `120` |
| `GITHUB_API_TIMEOUT_SECONDS` | Positive seconds; default `10` for activity clients |
| `GITHUB_CHECKOUT_TIMEOUT_SECONDS` | Positive seconds; default `120` |
| `SANDBOX_STAGING_DIRECTORY` | Existing absolute private directory; no symlink |
| `REVIEW_CHECK_ALLOWLIST_JSON` | JSON list of operator-approved check names, immutable images, exact command vectors, and optional maximum resources |

Each allowlist image must already contain its runtime and dependencies. Repository configuration
can select only exact allowlisted names, images, and commands and cannot exceed operator or platform
limits. Sandbox network stays disabled and no credentials enter check containers. See
[review checks](review-checks.md) for schema and path-filter behavior.
Per-repository branch and token/cost policy is set through the [policy API](repository-policy.md),
not environment variables. Defaults are a 100,000-token budget and 1,000,000 USD millionths.
OpenAI token counts are recorded when returned; unavailable billed cost stays unknown.

## Telemetry and tests

| Variable | Meaning |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` for host processes; `http://otel-collector:4318` inside Compose |
| `OTEL_METRICS_PORT`, `OTEL_HEALTH_PORT` | Local Compose published ports; defaults `8889`, `13133` |
| `API_PORT` | Local Compose API port; default `8000` |
| `TEST_DATABASE_URL` | Test-only PostgreSQL connection; tests create and remove isolated schemas |
| `RUN_DOCKER_SANDBOX_TESTS` | Set `1` to exercise the real Linux Docker sandbox |
| `SANDBOX_TEST_IMAGE` | Immutable test image ID or digest for that suite |

`APP_ENV` in the local template is descriptive; it is not a production-security switch.
The local collector does not publish 4318 to the host. For host export, provide a reachable
collector or a reviewed loopback-only port override. Unset the OTLP endpoint to disable export.

## Container and VM settings

`ACTIVITY_WORKER_IMAGE` names the provider image. The baseline local Compose file does not forward
every activity setting or mount a sandbox engine; a reviewed local override must supply these
before using that container for real reviews. The [VM manifest](../infra/deployment/compose.vm.yaml)
contains the complete provider environment and rootless socket/staging mounts.

The VM template additionally names immutable platform/service images, private bind/DNS/TLS
settings, distinct database password files, secret-reader group, rootless engine UID/GID/socket,
and backup directory. `GITHUB_PRIVATE_KEY_FILE` is the host secret source; the container sees
`GITHUB_PRIVATE_KEY_PATH=/run/secrets/github_private_key`. Follow [deployment](deployment.md)
and its preflight command instead of transferring host-local paths unchanged into containers.

Never commit filled environment files. Trusted VM/Docker operators can inspect container
environment secrets; private key files stay outside the checkout and build context.
