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

## Local configuration

Copy `.env.example` to `.env`. Never commit `.env`, API keys, GitHub private keys, or tokens.
Local containers must use development-only credentials.

Run the API after setting the required values in `.env`:

```text
uvicorn --factory pr_reliability_api.app:create_app_from_environment
```

Webhook startup requires `DATABASE_URL`, `OWNER_ID`, `GITHUB_INSTALLATION_ID`, and
`GITHUB_WEBHOOK_SECRET`.

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
`ACTIVITY_WORKER_IMAGE` must name an image built from this project that also installs a provider
package. `REVIEW_ACTIVITY_OPERATIONS_FACTORY` must use `module:factory` and return one complete
`ActivityOperations` value containing context, model, verification, publish, and terminal
operations. Registering partial activity sets on the same queue is not supported.

## Quality commands

Commands will be finalized with the first implementation issue. Expected checks are:

```text
ruff check .
ruff format --check .
pytest
```

Integration tests must use isolated databases and queues. End-to-end tests must use a test
GitHub App or recorded fixture, never a production repository.

Temporal workflow tests start the SDK's time-skipping test server. They exercise activity retries,
approval timeout, cancellation, signal-with-start supersession, continue-as-new, and history replay:

```text
uv run pytest workers/tests -q
```

CI runs these tests in the dedicated `temporal-workflow` job.

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

