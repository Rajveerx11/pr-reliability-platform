# Issue #44: GitHub login and sessions

Checkpoint: start from `5788388` on `codex/issue-44-github-login`. Source:
[#44](https://github.com/Rajveerx11/pr-reliability-platform/issues/44).
Decisions: DEC-005 ownership, DEC-009 human approval, DEC-015 production evidence.

Replace production shared-token access with GitHub App user authorization. Use an explicit
numeric account and user allowlist, separate reviewer/admin roles, live installation repository
access on every protected request, durable encrypted provider credentials, hashed opaque
sessions, expiry, rotation, CSRF protection, logout, and individual revocation. Approval events
must identify the stable GitHub user and existing ULID actor. Scope lists, counts, details, and
mutations to that user's current repositories. Provider failures deny access.

No change to webhook authentication or approval-bound publishing. No public hosting, account
provisioning, real GitHub approval, or merge in this task. Existing explicit bearer test fixtures
may remain injectable; the environment entrypoint must only support GitHub sessions.

Validation: login state/PKCE replay, cookies, expiry/rotation/revocation, CSRF, roles, owner and
repository isolation, audit identity, provider failures, migration and deployment checks, real
browser flow using test dependencies. Run Ruff, API/contracts/deployment tests and configured
Quality jobs: repository-shape, python-quality, temporal-workflow, temporal-workflow-windows,
sandbox-integration. No branch protection configured; all five jobs are delivery gates.
Independent review required before publication. User's task-to-pr invocation authorizes the
scoped commit, push, and PR; merge needs a separate request.


## Local delivery evidence

- Ruff check and format pass; JavaScript syntax checks pass.
- `uv run pytest apps/api/tests packages/contracts/tests infra/deployment/tests -q`: 200 passed,
  2 skipped on Windows with an isolated PostgreSQL 18 instance. Skips cover POSIX-only file rules.
- `uv run pytest --ignore=workers/tests -q`: 437 passed, 17 skipped, one known Windows Git-pack
  cleanup PermissionError in an imported production-operations test. The exact failing test also
  fails on unchanged main `5788388`; no worker implementation changed. Linux CI remains required.
- `uv build --wheel` and both Compose configuration validations pass.
- Playwright with a local TLS server, PostgreSQL, and fake GitHub boundary passed seven scenarios:
  login/callback, cookie/storage controls, repository and CSRF denial, individual approval,
  mobile layout, outage-safe logout after reload, and session expiry. Zero browser page errors.
  The completed logout request is reported as aborted when its handler navigates; cookie deletion
  and subsequent unauthorized reads were verified. Screenshots were inspected at desktop/mobile.
- Independent review found one outage/logout problem, which was fixed and verified. Final fresh
  review of backend, frontend, migration, deployment, documentation, and tests has no important
  unresolved findings.
- Live GitHub App authorization and Linux recovery are still #15 acceptance, not claimed by these
  local tests. Required next delivery gate: all five configured Quality jobs on the PR commit.
