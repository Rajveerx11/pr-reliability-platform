# Development Guide

## Requirements

- Python 3.12 or newer
- Git
- Docker with Compose
- GitHub CLI for repository administration
- Temporal development server or local container
- PostgreSQL local container

## Folder rules

- `apps/api/`: webhook and product API.
- `apps/web/`: approval and run interface.
- `workers/`: Temporal workflows and activities.
- `packages/contracts/`: shared versioned data shapes.
- `packages/proof_adapter/`: Proof of Work integration.
- `evals/golden_prs/`: frozen evaluation fixtures.
- `infra/`: local and deployment infrastructure.
- `migrations/`: PostgreSQL migrations.
- `plan/`: product plans and decisions.
- `docs/`: technical documentation.

Keep each file focused on one responsibility. Prefer small modules, but do not split one simple
idea across many tiny files. A file should be easy to understand in one sitting.

## Feature workflow

1. Open or choose one GitHub issue.
2. Confirm acceptance criteria and linked decision.
3. Create a focused branch.
4. Add tests with implementation.
5. Update relevant documentation.
6. Add one entry to `Changes.md` with issue, decision, and changed files.
7. Open a pull request linked to the issue.
8. Merge only after required checks pass and a human approves.

## Production work

Use [production readiness](production-readiness.md) and tracking issue #46 for delivery order.
Production provider wiring is tracked in issue #36. Repository-defined checks must follow
[review checks and CI boundary](review-checks.md). Each feature still needs its own issue before
implementation.

## Local configuration

Copy `.env.example` to `.env`. Never commit `.env`, API keys, GitHub private keys, or tokens.
Local containers must use development-only credentials.

Run the API after setting the required values in `.env`:

```text
uvicorn --factory pr_reliability_api.app:create_app_from_environment
```

Webhook and approval startup requires `DATABASE_URL`, `OWNER_ID`, `GITHUB_INSTALLATION_ID`,
`GITHUB_WEBHOOK_SECRET`, `APPROVAL_ACTOR_ID`, and `APPROVAL_REVIEWER_TOKEN`. Open
`/approval-inbox`, enter the configured reviewer token, and load findings. Keep this token outside
source control and rotate it like any other application secret.

Run the durable command dispatcher with `DATABASE_URL`, `TEMPORAL_ADDRESS`,
`TEMPORAL_NAMESPACE`, and `TEMPORAL_TASK_QUEUE` configured:

```text
pr-reliability-command-dispatcher
```

It drains committed `run.command_created` events, starts or supersedes the matching Temporal
workflow, and appends a dispatch receipt. Run one or more replicas; row locking prevents
concurrent delivery while stable command IDs make crash retries safe.

Repository deployment configuration launches the API, command dispatcher, Temporal workflow
worker, and provider activity worker as separate processes:

```text
docker compose --env-file .env -f infra/compose/compose.yaml up --build
```

The configured PostgreSQL and Temporal addresses must be reachable from their consuming services.
The local OpenTelemetry collector accepts OTLP/HTTP and exposes Prometheus metrics on port `8889`
and collector health on port `13133`. API liveness and dependency readiness are available at
`/health/live` and `/health/ready`. See `docs/observability.md` for trace and metric fields.
Set `HEALTH_CHECK_TIMEOUT_SECONDS` to bound each readiness dependency check; the default is two
seconds.
`ACTIVITY_WORKER_IMAGE` must name an image built from this project that also installs a provider
package. `REVIEW_ACTIVITY_OPERATIONS_FACTORY` must use `module:factory` and return one complete
`ActivityOperations` value containing context, model, verification, publish, and terminal
operations. Registering partial activity sets on the same queue is not supported.
The publish operation must use `GitHubReviewPublishOperation` with `GitHubRestReviewClient`.
Configure that client with a short-lived repository installation token and the numeric user ID of
the authenticated GitHub App bot. It first creates an unsubmitted `PENDING` pull request review
with the approved head SHA as `commit_id`. It rechecks the head, deletes the draft on drift, and
submits event `COMMENT` only after a match. It pages through every review and accepts a retry marker
only from that author when the commit, state, terminal marker, and full body match. An exact pending
draft is resumed or deleted after the same head check. It never trusts another author's marker, a
marker inside claim text, edited content, or a review bound to another commit, and never includes
tokens, review bodies, GitHub response bodies, or provider exception text in activity failures.

The client accepts only the standard `https://api.github.com` origin, preventing an installation
token from being sent to a caller-selected host. Enterprise GitHub support requires a future
explicit trusted-origin design.

Private version-one deployment uses `infra/deployment/compose.vm.yaml`, immutable image digests,
external secret files, private-IP TLS termination, local Prometheus, and a dedicated rootless
sandbox Docker engine. Follow `docs/deployment.md`; local Compose remains a development-only path.

## Quality commands

Run these required repository checks:

```text
ruff check .
ruff format --check .
pytest
```

Integration tests must use isolated databases and queues. End-to-end tests must use a test
GitHub App or recorded fixture, never a production repository.

Temporal workflow tests use the SDK's time-skipping server on Linux. On Windows, they use the local
dev server because the Windows time-skipping server can stall a query while Continue-As-New changes
runs. Both paths exercise retries, approval timeout, cancellation, supersession, and history replay:

```text
uv run pytest workers/tests -q
```

CI runs the full worker suite in the Linux `temporal-workflow` job. A focused
`temporal-workflow-windows` job runs the workflow regression module on every pull request and push
to `main`.

This platform-specific test harness keeps production timeouts unchanged and preserves the Linux
time-skipping coverage. [Issue #47](https://github.com/Rajveerx11/pr-reliability-platform/issues/47)
was closed after both paths passed repeatedly.

Sandbox unit tests use an injected fake Docker boundary and run with the normal worker suite. Real
isolation tests require a reachable Linux Docker engine and an immutable local image ID:

```text
docker build --file infra/sandbox/Dockerfile --tag pr-reliability-sandbox:test .
SANDBOX_TEST_IMAGE=$(docker image inspect --format '{{.Id}}' pr-reliability-sandbox:test) \
RUN_DOCKER_SANDBOX_TESTS=1 uv run pytest workers/tests/sandbox -q
```

CI runs real network, workspace-destruction, timeout, output, tmpfs, CPU, memory, and process-limit
checks in the dedicated `sandbox-integration` job. If Docker is missing or its daemon is not a
Linux engine, production verification raises `SandboxUnavailableError`; it never executes the
command on the host.

PostgreSQL integration tests require `TEST_DATABASE_URL`. Tests create a random schema, apply all
migrations, and remove that schema afterward. Example local value:

```text
postgresql://postgres:postgres@localhost:5432/postgres
```
