# Development guide

## Requirements and install

Use Python 3.12, uv, Git, PostgreSQL, and a Temporal development server. Python 3.12 is the CI
baseline; the package permits newer Python versions, but they are not the release evidence.
Real verification requires a Linux Docker engine with resource-limit support. GitHub CLI is
needed for issue and PR administration, not application runtime.

From the repository root:

```text
uv sync --frozen --extra dev --python 3.12
```

Copy `.env.example` to `.env`, fill the required values, and keep it untracked. On PowerShell:

```powershell
Copy-Item -LiteralPath .env.example -Destination .env
```

On POSIX shells use `cp .env.example .env`. The copy command is for first setup; preserve an
existing `.env`. Never commit keys, passwords, or filled secret files. See [configuration](configuration.md).

## Start the local control plane

Start PostgreSQL and Temporal separately and configure reachable addresses in `.env`.
The baseline Compose file does not provide either server. Apply all five migrations:

```text
uv run --env-file .env python -m pr_reliability_api.migrate
```

Each long-running process below runs in its own terminal:

```text
uv run --env-file .env uvicorn --factory pr_reliability_api.app:create_app_from_environment --host 127.0.0.1 --port 8000
uv run --env-file .env pr-reliability-repository-sync
uv run --env-file .env pr-reliability-command-dispatcher
uv run --env-file .env pr-reliability-workflow-worker
```

The API also verifies/applies migrations on environment-factory startup. An explicit migration
step avoids races when starting other database-backed processes. The sync process needs the
configured GitHub App ID, installation ID, owner, and absolute private-key path; it imports
repositories before any PR arrives. To verify one sync and exit:

```text
uv run --env-file .env pr-reliability-repository-sync --once
```

Subscribe a dedicated test GitHub App to PR, installation, and installation-repository events.
Wait for successful sync before sending test PRs. `/health/ready` returns 503 until PostgreSQL,
Temporal, and fresh installation inventory are available. `/health/live` only proves process
liveness. See [repository policy](repository-policy.md) and [API reference](api.md).

Open `http://127.0.0.1:8000/dashboard` or `/approval-inbox` and enter the configured reviewer token.
Repository policy is an authenticated API today; the repository/history UI remains #38.

## Production activity process

On the approved Linux provider host/container, start:

```text
uv run --env-file .env pr-reliability-activity-worker
```

Use `REVIEW_ACTIVITY_OPERATIONS_FACTORY=pr_reliability_workers.providers:create_operations`.
The factory supplies context, OpenAI analysis, sandbox verification, approved GitHub publishing,
and terminal persistence. Configure an explicit model, bot user ID, immutable sandbox image,
JSON command vector, private staging directory, and dedicated sandbox engine. Do not register
partial activity sets on the same queue. Missing isolation fails closed; Windows development
can test the control plane and fixtures, but does not replace the production Linux sandbox.

The dispatcher drains durable run and approval commands. It rechecks repository policy before
dispatch and uses stable IDs so retries do not create duplicate work. Publishing stages a
commit-bound pending review and submits it only after approval and another head check. Detailed
boundaries live in [architecture](architecture.md) and [security](security.md).

## Container configurations

The development process manifest is `infra/compose/compose.yaml`:

```text
docker compose --env-file .env --file infra/compose/compose.yaml config --quiet
```

The manifest defines the API, sync, dispatcher, workflow/activity workers, and collector. PostgreSQL and
Temporal must be reachable from containers; `localhost` inside a container is that container.
The activity service in this baseline manifest needs a reviewed override with the provider
environment and sandbox socket/staging mounts before it can perform real reviews. Compose's
`--env-file` does not inject every template value into every service. The complete private Linux
VM stack is documented in [deployment](deployment.md); do not describe the baseline manifest
alone as a working production deployment.

Start with `up --build` only after validating the combined manifest and reviewed local override.
Use container-reachable database and Temporal addresses and
`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` in the Compose environment.

Use `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` for host processes and the collector's
service hostname inside Compose. The local collector exposes metrics on 8889 and health on 13133.
It does not publish OTLP port 4318 to the host. Host-process export needs a separate reachable
collector or a reviewed override that binds `127.0.0.1:4318:4318`; telemetry export is optional.

## Tests and quality

```text
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

Set `TEST_DATABASE_URL` to an isolated development database. Each integration test creates a
random schema, applies migrations, and removes the schema afterward; never point it at production.
Example PowerShell setup:

```powershell
$env:TEST_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/pr_reliability_test'
uv run pytest apps/api/tests packages/contracts/tests -q
uv run pytest workers/tests -q
```

On POSIX use `export TEST_DATABASE_URL=...`. Without a test database, local database tests skip;
CI requires it. Linux workflow tests use Temporal's time-skipping server. Windows workflow tests
use the local dev server to avoid the supersession stall fixed by #47. The configured Windows CI
job runs only `workers/tests/test_pull_request_review_workflow.py`.

A full Windows suite has a known pre-existing Git-pack cleanup failure in the production-operations
fixture, reproduced on `09da81a`. It does not fail Linux CI. Psycopg async tests use a selector event
loop on Windows. Latest dated evidence is in [status](status.md); do not infer a full-suite Windows
pass from the focused workflow job.

For real sandbox tests, build the fixture image and obtain its immutable ID. PowerShell:

```powershell
docker build --file infra/sandbox/Dockerfile --tag pr-reliability-sandbox:test .
$env:SANDBOX_TEST_IMAGE = docker image inspect --format '{{.Id}}' pr-reliability-sandbox:test
$env:RUN_DOCKER_SANDBOX_TESTS = '1'
uv run pytest workers/tests/sandbox -q
```

POSIX:

```sh
docker build --file infra/sandbox/Dockerfile --tag pr-reliability-sandbox:test .
export SANDBOX_TEST_IMAGE=$(docker image inspect --format '{{.Id}}' pr-reliability-sandbox:test)
RUN_DOCKER_SANDBOX_TESTS=1 uv run pytest workers/tests/sandbox -q
```

These tests need a reachable Linux Docker engine. The dedicated CI job tests real network,
workspace destruction, timeouts, output, tmpfs, CPU, memory, and process limits.

## Repository and delivery rules

Plans live in `plan/`; docs in `docs/`; feature code in its owning `apps/`, `workers/`, or
`packages/` directory. Tests live beside their code area. Migrations live in `migrations/`,
frozen tasks in `evals/golden_prs/`, and infrastructure in `infra/`.

Choose one GitHub issue, confirm acceptance and the decision in [plan/v1.md](../plan/v1.md), then
implement on a focused branch. Update docs and `Changes.md`, link the PR to the issue and decision,
and run applicable checks. Publishing comments, patches, or commits requires explicit human
approval under [AGENTS.md](../AGENTS.md). Merge only after checks pass and a human authorizes it.
Production priorities remain in [production readiness](production-readiness.md).
