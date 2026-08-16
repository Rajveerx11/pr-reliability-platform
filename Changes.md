# Changes

This file records repository changes. Each entry links changes to the decision that caused
them and names the affected files.

## Unreleased

### Added approval-bound idempotent GitHub review publishing

- Issue: [#12](https://github.com/Rajveerx11/pr-reliability-platform/issues/12)
- Decision: [DEC-009 — Human approval](plan/v1.md#dec-009--require-human-approval-before-every-external-write)
- Reason: Approved findings need one retry-safe, commit-bound GitHub review summary that rejects
  stale or incomplete approval state and leaves a safe audit trail.
- Changed files:
  - `workers/src/pr_reliability_workers/activities/publish.py` — database approval and head checks,
    exclusive recoverable publication claims, stable marker recovery, sanitized failures, and
    immutable publish-payload fingerprints with bounded success/failure audit.
  - `workers/src/pr_reliability_workers/activities/github.py` — repository-scoped GitHub REST
    client that creates commit-bound review summaries and recovers them with complete pagination,
    authenticated App-author, commit, terminal-marker, and exact-body checks.
  - `workers/tests/test_publish_activity.py` and `test_github_comment_client.py` — unapproved,
    stale, concurrent retry, crash-recovery, edited-body, cross-marker, privacy, pagination,
    ownership, and audit coverage.
  - `packages/contracts/` — one-to-one finding/approval mapping and stable publish-key validation.
  - `migrations/0004_bind_external_action_payload.sql` — required canonical payload fingerprint
    on every reserved external action.
  - `pyproject.toml` and `uv.lock` — production HTTP client dependency for GitHub publishing.
  - `docs/architecture.md`, `docs/security.md`, and `docs/development.md` — publish flow, safety
    boundary, and provider requirements.
  - `plan/v1.md` — version-one output names the commit-bound review summary.

### Added human approval inbox

- Issue: [#11](https://github.com/Rajveerx11/pr-reliability-platform/issues/11)
- Decision: [DEC-009 — Human approval](plan/v1.md#dec-009--require-human-approval-before-every-external-write)
- Reason: Reviewers need one authenticated place to inspect evidence and record a decision bound
  to the analyzed commit without publishing from the browser.
- Changed files:
  - `apps/web/approval_inbox.html` — responsive finding review and approve or reject interface.
  - `apps/api/src/pr_reliability_api/approvals/` — owner-scoped inbox reads, bearer authorization,
    stale-commit protection, idempotent decisions, and append-only audit events.
  - `packages/contracts/src/pr_reliability_contracts/approvals.py` — inbox, decision request, and
    receipt contracts.
  - `apps/api/tests/test_approval_inbox.py` and `packages/contracts/tests/test_approvals.py` —
    authorization, evidence display, commit binding, idempotency, and no-publish coverage.
  - `workers/src/pr_reliability_workers/dispatch.py` and
    `workers/tests/test_command_dispatcher.py` — durable, retry-safe delivery of recorded
    decisions to the waiting Temporal workflow.
  - `.env.example`, `pyproject.toml`, `infra/compose/compose.yaml`, and
    `.github/workflows/quality.yml` — reviewer configuration and packaged browser shell.
  - `docs/architecture.md`, `docs/security.md`, and `docs/development.md` — approval boundary and
    operating instructions.

### Added disposable pull request command sandbox

- Issue: [#9](https://github.com/Rajveerx11/pr-reliability-platform/issues/9)
- Decision: [DEC-008 — Disposable sandbox](plan/v1.md#dec-008--run-untrusted-commands-inside-a-real-sandbox)
- Reason: Pull request tests must run with enforced isolation and must never fall back to the host.
- Changed files:
  - `workers/src/pr_reliability_workers/sandbox/` — immutable-image Docker runner with network,
    privilege, CPU, memory, process, filesystem, staging, output, and time limits plus forced cleanup.
  - `workers/src/pr_reliability_workers/activities/` — mandatory verification adapter that admits
    only successful sandbox execution results to approval and records failed bounded evidence.
  - `workers/tests/sandbox/` — fake-runtime failure coverage and real Docker isolation tests.
  - `infra/sandbox/Dockerfile` — pinned Python fixture image for the real CI boundary.
  - `.github/workflows/quality.yml` — dedicated Linux Docker sandbox integration job.
  - `docs/architecture.md`, `docs/security.md`, and `docs/development.md` — trust boundary,
    operational requirements, failure behavior, and local test guidance.

### Added durable Temporal pull request workflow

- Issue: [#8](https://github.com/Rajveerx11/pr-reliability-platform/issues/8)
- Decision: [DEC-004 — Temporal workflows](plan/v1.md#dec-004--use-temporal-for-durable-workflows)
- Reason: Long review runs need durable retries, explicit waits and cancellation, and safe
  replacement when a pull request receives a new commit.
- Changed files:
  - `workers/src/pr_reliability_workers/workflows/` — deterministic review orchestration,
    approval signals, explicit terminal outcomes, and continue-as-new supersession.
  - `workers/src/pr_reliability_workers/activities/` — stable activity names and idempotency-key
    boundary for context, analysis, verification, publish, and terminal persistence.
  - `workers/src/pr_reliability_workers/dispatch.py` and `worker.py` — PostgreSQL outbox consumer,
    atomic signal-with-start dispatch, production dispatcher/workflow/activity-worker entry
    points, provider factory contract, and combined test-worker assembly.
  - `packages/contracts/src/pr_reliability_contracts/runs.py` — monotonic run generation in each
    start command for ordered supersession.
  - `migrations/0003_order_run_generations.sql` — safe upgrade that renumbers existing runs and
    enforces monotonic generation per pull request.
  - `workers/tests/` — retry, timeout, cancellation, supersession, and replay integration tests.
  - `.github/workflows/quality.yml` — dedicated Temporal execution, outbox, and replay CI job with
    immutable action references.
  - `infra/compose/` — shared application image and separate API, dispatcher, workflow-worker, and
    externally supplied provider activity-worker processes.
  - `pyproject.toml` and `uv.lock` — Temporal SDK and packaged worker runtime.

### Added signed and deduplicated GitHub webhook intake

- Issue: [#7](https://github.com/Rajveerx11/pr-reliability-platform/issues/7)
- Decision: [DEC-001 — Approval-first GitHub App](plan/v1.md#dec-001--build-an-approval-first-github-app)
- Reason: Pull request commands must start only after signature verification and delivery replay
  protection.
- Changed files:
  - `apps/api/src/pr_reliability_api/app.py` and `webhooks/` — runnable API, signature-first
    validation, supported pull request actions, stale-event protection, reopen generations,
    atomic delivery deduplication, and run-command persistence.
  - `migrations/0002_github_webhook_deliveries.sql` — durable delivery identity and metadata.
  - `apps/api/tests/test_github_webhooks.py` — real PostgreSQL and FastAPI integration tests.
  - `pyproject.toml` and `uv.lock` — FastAPI, Uvicorn, and test-client dependencies.

### Added deterministic pull request context selection

- Issue: [#5](https://github.com/Rajveerx11/pr-reliability-platform/issues/5)
- Decision: [DEC-002 — Single-agent baseline](plan/v1.md#dec-002--start-with-one-agent)
- Reason: The review agent needs reproducible changed-file and direct-dependency context that
  cannot exceed its configured token budget.
- Changed files:
  - `workers/src/pr_reliability_workers/context/` — safe paths, local Python dependency
    resolution, changed-file priority, truncation, and budget enforcement.
  - `workers/tests/context/test_selector.py` — priority, determinism, exclusion, dependency,
    truncation, and budget tests.
  - `pyproject.toml` — worker imports and wheel packaging.
  - `docs/architecture.md` — context-selection boundary.
### Added PostgreSQL product schema and migration runner

- Issue: [#3](https://github.com/Rajveerx11/pr-reliability-platform/issues/3)
- Decision: [DEC-005 — PostgreSQL ownership](plan/v1.md#dec-005--use-postgresql-with-stable-ownership-fields)
- Reason: Product records need stable public identities, tenant-safe relationships, and database
  constraints that make retries idempotent.
- Changed files:
  - `migrations/0001_initial.sql` — owned product tables, external-action idempotency,
    append-only audit enforcement, constraints, and indexes.
  - `apps/api/src/pr_reliability_api/db/` — ordered, checksummed migration runner.
  - `apps/api/tests/test_migrations.py` — real PostgreSQL migration and constraint tests.
  - `pyproject.toml` and `uv.lock` — PostgreSQL driver plus packaged API and migrations.
  - `docs/architecture.md` and `docs/development.md` — persistence and test guidance.
  - `.github/workflows/quality.yml` — PostgreSQL service for mandatory migration tests.

### Added first frozen golden pull request corpus

- Issue: [#4](https://github.com/Rajveerx11/pr-reliability-platform/issues/4)
- Decision: [DEC-002 — Single-agent baseline](plan/v1.md#dec-002--start-with-one-agent)
- Reason: Version one needs a stable, deterministic set of known defects before agent quality
  can be measured or compared.
- Changed files:
  - `evals/golden_prs/corpus.py` — strict loader, safe paths, protected verifier runner, and
    stable corpus fingerprint.
  - `evals/golden_prs/tasks/` — ten broken fixtures, reference fixes, metadata, and verifiers.
  - `evals/golden_prs/corpus.sha256` — frozen corpus fingerprint.
  - `evals/tests/test_golden_corpus.py` — corpus contract and verifier tests.
  - `docs/evaluation.md` — task coverage and freeze rules.
  - `pyproject.toml` — evaluation test discovery.
### Added strict version-one contracts and run states

- Issue: [#2](https://github.com/Rajveerx11/pr-reliability-platform/issues/2)
- Decision: [DEC-006 — Versioned JSON contracts](plan/v1.md#dec-006--use-versioned-json-contracts)
- Reason: Services and agents need replayable messages that reject undeclared or incomplete
  input before work starts.
- Changed files:
  - `packages/contracts/src/pr_reliability_contracts/` — shared base types, run commands and
    states, findings, evidence, approvals, publish commands, and GitHub webhook envelope.
  - `packages/contracts/tests/` — round-trip, strict-input, validation, and state tests.
  - `pyproject.toml` and `uv.lock` — Pydantic dependency, reproducible versions, package build
    settings, and test import paths.
  - `.github/workflows/quality.yml` — lint, format, and test jobs.
  - `docs/architecture.md` — contract identity and run state rules.

## 2026-08-12 — Repository foundation

### Added private project repository

- Decision: [DEC-010 — Repository structure](plan/v1.md#dec-010--use-clear-code-plan-and-documentation-boundaries)
- Reason: Keep code, plans, documentation, evaluations, and infrastructure easy to find.
- Changed files:
  - `README.md`
  - `AGENTS.md`
  - `.gitignore`
  - `.editorconfig`
  - `.env.example`
  - `pyproject.toml`
  - Code boundary folders under `apps/`, `workers/`, and `packages/`
  - Supporting folders under `evals/`, `infra/`, `migrations/`, and `scripts/`

### Added version-one plan and decision log

- Decisions: [DEC-001 through DEC-013](plan/v1.md#decisions)
- Reason: Freeze important product, workflow, safety, data, and delivery choices before code.
- Changed files:
  - `plan/README.md`
  - `plan/v1.md`
  - `plan/interactive.html`

### Added project documentation

- Decisions: [DEC-005](plan/v1.md#dec-005--use-postgresql-with-stable-ownership-fields), [DEC-008](plan/v1.md#dec-008--run-untrusted-commands-inside-a-real-sandbox), [DEC-009](plan/v1.md#dec-009--require-human-approval-before-every-external-write), [DEC-010](plan/v1.md#dec-010--use-clear-code-plan-and-documentation-boundaries)
- Reason: Record architecture, setup, security, and evaluation rules outside source code.
- Changed files:
  - `docs/README.md`
  - `docs/architecture.md`
  - `docs/development.md`
  - `docs/security.md`
  - `docs/evaluation.md`

### Added GitHub issue workflow

- Decision: [DEC-011 — GitHub issues](plan/v1.md#dec-011--track-every-feature-with-a-github-issue)
- Reason: Give each feature a clear scope, acceptance criteria, dependencies, and history.
- Changed files:
  - `.github/ISSUE_TEMPLATE/feature.yml`
  - `.github/ISSUE_TEMPLATE/bug.yml`
  - `.github/ISSUE_TEMPLATE/config.yml`
  - `.github/workflows/quality.yml`

### Created version-one milestone and feature issues

- Decision: [DEC-011 — GitHub issues](plan/v1.md#dec-011--track-every-feature-with-a-github-issue)
- Reason: Make feature scope, acceptance criteria, dependency order, and progress visible.
- GitHub changes:
  - Created milestone `v0.1.0`.
  - Created issues [#1 through #15](https://github.com/Rajveerx11/pr-reliability-platform/issues).
  - Closed [#1](https://github.com/Rajveerx11/pr-reliability-platform/issues/1) after foundation commit `fbeb0da`.
- Changed files:
  - `plan/v1.md` — added issue and dependency map.
  - `Changes.md` — recorded milestone and issue creation.
