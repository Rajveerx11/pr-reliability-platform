# Current status

Status date: 2026-09-03

The version-one core is merged to `main`. The service is not production ready. Production
provider wiring, repository onboarding, GitHub login, complete dashboard history, signed release
artifacts, real-provider evaluation, and private Linux VM acceptance remain open.

## Product boundary

This is an approval-first AI pull request reviewer. It uses selected CI-style checks as evidence.
It is not a general CI/CD system and does not replace GitHub Actions deployment pipelines. See
[review checks and CI boundary](review-checks.md).

## Implemented in the repository

- Versioned contracts, PostgreSQL migrations, and owner-scoped records.
- Signed and deduplicated GitHub webhook intake.
- Durable Temporal workflow with retries, cancellation, and supersession.
- Deterministic context selection and provider-neutral structured findings.
- Disposable Docker sandbox and Proof of Work evidence gate.
- Human approval and idempotent publishing boundaries.
- Traces, metrics, health checks, approval inbox, and private operations dashboard.
- Frozen ten-task evaluation corpus and deterministic harness replay.
- Private-IP TLS deployment configuration, backup, restore, and rollback tools.

## Production backlog

[Issue #46](https://github.com/Rajveerx11/pr-reliability-platform/issues/46) is the production
tracking issue. Its work includes #36 through #45: production providers, repository sync and
policy, dashboard history, analytics, Check Runs, review checks, bounded evidence, runner
operations, GitHub login, and signed images.

Existing acceptance issues #14 and #15 remain open. See
[production readiness](production-readiness.md) for the order and release gate.

## Verification snapshot

- Required GitHub checks on `main`: passing.
- Local Python 3.12 lint and format checks: passing.
- Local Windows Python 3.12 test run: 218 passed and 72 skipped.
- Linux CI runs the full Temporal worker suite with time skipping. Windows CI runs the focused
  workflow regression module against the local dev server.
- Issue #47 is closed.
- Issue #12 is closed by approval-bound, stale-safe, exactly-once review publication acceptance.
- No production timeout was increased.

Do not describe the service as deployed or model quality as measured until the production exit
records exist.
