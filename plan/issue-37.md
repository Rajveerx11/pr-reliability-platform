# Repository synchronization and policy

Issue: [#37](https://github.com/Rajveerx11/pr-reliability-platform/issues/37)
Status: merged and issue closed on 2026-09-07.
PR: [#51](https://github.com/Rajveerx11/pr-reliability-platform/pull/51).
Merge commit: `675257ade505d4116c6078147a68c35bafc8f424`; final feature commit: `5dff0ac`.
Base commit: `09da81a`. The temporary branch and worktrees were removed after merge.
This is a historical delivery record, not an active checkout instruction.
Decisions: [DEC-001](v1.md#dec-001--build-an-approval-first-github-app),
[DEC-005](v1.md#dec-005--use-postgresql-with-stable-ownership-fields),
[DEC-014](v1.md#dec-014--limit-ci-style-work-to-review-evidence).

## Acceptance

Import all repositories selected for the configured installation. Reconcile on startup and
every minute. Apply removal and suspension immediately; confirm additions and restoration
through GitHub before granting access. Keep owner-scoped inventory, policy, timestamps, and
append-only audit records. Enforce exact base-branch filters and token/cost budgets before
creating or dispatching a review. Preserve existing policy across reconciliation.

## Boundaries

One configured installation per private owner. No GitHub login or repository history UI yet
(#44 and #38). Verification profile is persisted as `default`, the existing operator-controlled
sandbox; additional profiles and repository-defined commands belong to #41. No GitHub comments
or source mutations are introduced by synchronization.

Unknown inventory blocks admission. Sync expires after 15 minutes without success. Reconciliation
uses complete bounded snapshots and rejects a snapshot when an intervening lifecycle event or
another reconciliation changes its revision. Delayed granting events cannot restore access.

## Verification checkpoint

Implementation testing used an isolated PostgreSQL cluster on port 55437 with separate test
schemas. That temporary service is stopped; configure a new test database for future work. Test command: `uv run pytest --ignore=workers/tests` and `uv run pytest workers/tests -q`.
Quality commands: `uv run ruff check .`, `uv run ruff format --check .`, `uv build`.
CI jobs: repository-shape, python-quality, temporal-workflow, temporal-workflow-windows,
sandbox-integration. No branch-protection required-check list is configured.
Independent review and CI completed before human merge. Latest merged-baseline evidence is in
[docs/status.md](../docs/status.md).

## Local evidence

- Focused policy, dispatcher, provider, backup, and deployment-health suite: 71 passed.
- Policy contracts: 11 passed; existing webhook/migration/dispatcher suite: 51 passed.
- Full local collection: 384 passed, 17 skipped, one existing Windows cleanup failure.
  The failing production-operations fixture cannot delete a read-only Git pack index. The same
  test fails on untouched base commit `09da81a`; production Linux CI is the acceptance check.
- Independent review: no important unresolved findings after audit-detail and readiness fixes.
- Greptile: 5/5; sync-health finding resolved. All five Quality jobs passed on final feature and merge commits.
- Follow-up PostgreSQL/policy/readiness suite: 33 passed, including owner isolation and expiry.
- Lint, formatting, Python package build, and both Compose configurations pass locally.
