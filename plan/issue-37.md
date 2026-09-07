# Repository synchronization and policy

Issue: [#37](https://github.com/Rajveerx11/pr-reliability-platform/issues/37)
Base commit: `09da81a` (production operations merged).
Branch: `codex/issue-37-repository-policy`.
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

Local PostgreSQL integration database uses an isolated cluster on port 55437. Tests use separate
schemas. Test command: `uv run pytest --ignore=workers/tests` and `uv run pytest workers/tests -q`.
Quality commands: `uv run ruff check .`, `uv run ruff format --check .`, `uv build`.
CI jobs: repository-shape, python-quality, temporal-workflow, temporal-workflow-windows,
sandbox-integration. No branch-protection required-check list is configured.
Independent review and green CI are required before handoff. Merge remains a human action.

## Local evidence

- Focused policy, dispatcher, provider, backup, and deployment-health suite: 71 passed.
- Policy contracts: 11 passed; existing webhook/migration/dispatcher suite: 51 passed.
- Full local collection: 384 passed, 17 skipped, one existing Windows cleanup failure.
  The failing production-operations fixture cannot delete a read-only Git pack index. The same
  test fails on untouched base commit `09da81a`; production Linux CI is the acceptance check.
- Independent review: no important unresolved findings after adding per-repository audit details.
- Lint, formatting, Python package build, and both Compose configurations pass locally.
