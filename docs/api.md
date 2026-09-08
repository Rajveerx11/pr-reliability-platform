# API reference

Baseline: `675257a`, verified 2026-09-07. FastAPI exposes `/openapi.json` and `/docs` for the
running application's exact request and response schemas. Keep the service on its private origin.

## Authorization

Product APIs require a GitHub session cookie. Mutations also require the exact configured
`Origin` and `X-CSRF-Token`; the client obtains CSRF proof from `/auth/session`. Missing or expired
sessions return 401. Disallowed users return 403. Cross-owner or inaccessible repository lookups
return no data or 404. Policy changes and user revocation require an administrator. Page shells
contain no private data. See [authentication](authentication.md) for login and session routes.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/health/live` | API process liveness; no token |
| GET | `/health/ready` | Production DB, Temporal, and sync readiness; no token |
| POST | `/webhooks/github` | HMAC-authenticated GitHub event intake |
| GET | `/api/repositories` | Owner-scoped installation inventory |
| PUT | `/api/repositories/{repository_id}/policy` | Complete versioned policy replacement |
| GET | `/api/dashboard/overview` | Owner-scoped run totals and latency summaries |
| GET | `/api/dashboard/runs` | Paginated run summaries and supported filters |
| GET | `/api/dashboard/runs/{run_id}` | Run timeline, findings, and evidence |
| GET | `/api/approval-inbox` | Findings on current heads awaiting approval |
| POST | `/api/approval-inbox/{finding_id}/decision` | Immutable approve/reject decision |
| GET | `/dashboard` | Read-only run dashboard shell |
| GET | `/approval-inbox` | Human approval interface shell |

Product routes are registered when approval settings are provided; the production environment
factory requires those settings. No public rerun or repository-sync trigger endpoint is
implemented. GitHub Check Run reruns arrive through the signed webhook. Use
`pr-reliability-repository-sync --once` for an operator sync.

## Webhook contract

Send `X-Hub-Signature-256`, `X-GitHub-Delivery`, and `X-GitHub-Event`. The signature is
`sha256=<HMAC-SHA256 of exact request bytes>`. Validation happens before JSON decoding.

- `pull_request`: `opened`, `reopened`, `synchronize`, `closed`.
- `check_run`: `rerequested`, or `requested_action` with identifier `rerun`, only for this App's
  persisted completed check identity and current pull request head.
- `installation`: `created`, `deleted`, `suspend`, `unsuspend`, `new_permissions_accepted`.
- `installation_repositories`: `added`, `removed`.

Missing headers or invalid/unsupported payloads return 422; invalid signatures return 401;
other installation IDs return 403. Successful deliveries return `accepted`, `duplicate`, and
`command_id`. Acceptance does not guarantee a review: policy blocks, lifecycle events, closed
PRs, and duplicate deliveries can return a null command ID. Full delivery replay never queues
a second command. See [repository policy](repository-policy.md) for recovery behavior.

## Policy and approval

Inventory supports `limit` (1–100, default 50) and `after` (public-ID cursor), returning
`schema_version`, `repositories`, and `next_cursor`. Policy schema version `1` supports exact
base-branch names, a positive token budget, nonnegative integer USD-millionth cost, enabled state,
and a verification profile. Omitted policy fields take their schema defaults; send the complete
policy to avoid resetting existing values. Only `default` is available. See the complete
[request example](repository-policy.md#private-api).

Approval decisions include the shown head SHA and are validated against current PR/run state.
The API stores the decision and durable signal; it does not publish to GitHub. Publishing happens
later only after verification and approval. See [approval contracts](../packages/contracts/src/pr_reliability_contracts/approvals.py)
and [security](security.md#github-review-publishing) for replay and stale-head rules.

## Readiness

The production factory adds `database`, `workflow`, and `repository_sync` dependency results.
All must be `ready` for HTTP 200. Any unavailable dependency returns 503 with `status: not_ready`.
Sync is unavailable before the first successful reconciliation or after 15 minutes without one.
Details and troubleshooting are in [observability](observability.md#health).
