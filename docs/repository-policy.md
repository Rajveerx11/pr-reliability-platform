# Repository inventory and review policy

The GitHub App installation defines which repositories the service can access. The policy
defines which of those repositories receive reviews. Both are checked before creating a run
and again before dispatching its queued command.

## Operation

Apply migrations before starting `pr-reliability-repository-sync`. The Compose manifests run
this process continuously. It imports repositories at startup and reconciles every 60 seconds.
For a one-time check, run `pr-reliability-repository-sync --once`.

Required environment: `DATABASE_URL`, `OWNER_ID`, `GITHUB_INSTALLATION_ID`, `GITHUB_APP_ID`, and
`GITHUB_PRIVATE_KEY_PATH`. The private key must be an absolute path to a private mounted file.
The API does not need this key. The sync process needs outbound HTTPS to GitHub, database access,
and no model key or Docker socket.

Subscribe the GitHub App to `installation` and `installation_repositories` events alongside
`pull_request`. Signature validation and the configured installation allowlist protect intake.
Inventory uses a separate short-lived token with only metadata read access across the selected
installation repositories. Review checkout and publishing retain their existing scoped tokens.
See [GitHub installation endpoints](https://docs.github.com/en/rest/apps/installations) and
[GitHub App endpoints](https://docs.github.com/en/rest/apps/apps).

Reconciliation reads at most 10,000 repositories in pages of 100. Errors, redirects, incomplete
pages, duplicate IDs, and changed totals reject the snapshot. An intervening lifecycle delivery
or successful sync invalidates an in-flight snapshot. Revocations take effect immediately.
Created, added, and restored events request reconciliation; they never grant access themselves.
The next periodic sync confirms the current GitHub state.

## Private API

Use the existing reviewer bearer token. `GET /api/repositories?limit=50&after=<cursor>` returns
only the configured owner's repositories, with stable public IDs and `next_cursor`. The maximum
page size is 100. Inventory includes access state, installation state, default branch, enabled
flag, policy, last sync, last webhook, and last admitted review time.

Replace the complete policy with `PUT /api/repositories/{public_id}/policy`:

```json
{
  "schema_version": "1",
  "enabled": true,
  "enabled_branches": ["main", "release"],
  "token_budget": 100000,
  "cost_budget_usd_micros": 1000000,
  "verification_profile": "default"
}
```

Branch names match the PR base branch exactly. An empty list permits every base branch.
Budgets are copied to the run and durable command. Lowering a budget prevents older queued
commands with larger budgets from starting. Cost values are integer USD millionths.
`default` is the only supported verification profile until #41 adds repository checks; it uses
the existing operator-selected sandbox image and command. Policy never accepts shell commands.

Set `enabled` to false to pause reviews. GitHub removal and suspension override this flag.
Reconciliation preserves policy, including pauses, through removal and restoration. Policy changes
record the configured reviewer actor and complete new policy in an append-only owner-scoped audit.
Repeated identical policy replacements and webhook deliveries do not create duplicate changes.

## Admission and rollout

Migration starts existing repositories as pending. Run the first sync before sending test PRs.
No review starts for unknown, removed, paused, suspended, deleted, or stale installations.
If synchronization fails for 15 minutes, new reviews stop until it recovers. Existing running
reviews are not cancelled by a policy change; existing approval and exact-head publication
checks still apply. No findings are published by this feature.

Production `/health/ready` includes a `repository_sync` dependency. It returns HTTP 503 when the
configured owner's installation has never synchronized or its last successful sync is older
than 15 minutes. Deployment health consumes this readiness response, so a running process with
persistent authentication, network, or database failures cannot keep the deployment healthy.
Freshly confirmed suspended or deleted installations remain operationally healthy; their access
restrictions intentionally block reviews.

Webhooks received while blocked are recorded but do not queue a deferred review. After restoring
access, use a new PR event (for example a new commit or reopening the PR) to request a review.
Replaying the same delivery remains a no-op. Closed PR events still update known PR state while
reviews are paused, so stale approvals remain blocked.

Rollback: stop admission and the sync process, then restore the previous compatible release and
database backup. Do not remove audit rows or silently bypass the new admission checks.
