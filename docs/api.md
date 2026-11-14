# API reference

Source checked 2026-11-14 against `main` at `d1662e2` plus #65 hardening. FastAPI exposes `/openapi.json` and `/docs` for the
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
| GET | `/api/findings/approved.jsonl` | Session-scoped approved structured findings export |
| GET | `/api/evidence/runs/{run_id}` | Retained sandbox artifact references and expiry |
| GET | `/api/evidence/{reference}` | Decrypted redacted artifact; `download=true` attaches JSON |
| GET | `/api/operations/overview` | Administrator runner, queue and alert snapshot |
| GET | `/api/operations/metrics` | Administrator Prometheus text metrics |
| POST | `/api/operations/runners/{runner_id}/drain` | Administrator drain request; returns 202 or 404 |
| GET | `/operations` | Operations shell; private data requires administrator access |

Product routes are registered when approval settings are provided; the production environment
factory requires those settings. Evidence routes additionally require evidence settings; the
production factory requires these too. Evidence reads return 404 outside the live repository
scope, 410 after expiry, and 503 when decryption fails. Operations mutations require CSRF.
Static assets live under `/auth/assets/`, `/dashboard/assets/` and `/operations/assets/`.
No public rerun or repository-sync trigger endpoint is
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

## Approved findings export

`GET /api/findings/approved.jsonl` returns `application/x-ndjson`, one JSON object per approved
finding, ordered by decision time and approval ID. It uses the existing GitHub session and live
owner/repository authorization. An empty export is an empty body. Responses disable caching.

Fields are `repository`, `pr_number`, `head_sha`, `category`, `severity`, `approval_decision`,
`approved_at` (UTC), and `pr_outcome`. The head is the approved run's head, including historical
approvals after the PR advances. Pending and rejected findings are excluded. No confidence,
claim prose, evidence, commands, exit codes, actor details or approval reasons are exported.
Category and severity remain model judgements that a human approved, not verified facts.

`pr_outcome` is currently `null`: storage tracks open/closed but does not persist merged versus
closed without merging. This endpoint exports approval history, not a publication receipt or
a learning journal. Journal ingestion, deduplication and feedback are not built. The current
single-owner export is buffered in memory; streaming or pagination should precede large histories.

Finding objects in other APIs label model evidence as `claimed`. Separate sandbox artifacts
can have execution results. Proof of Work v0.2.0 is detector-only; it executes no tests.

## Readiness results

The production factory adds `database`, `workflow`, and `repository_sync` dependency results.
All must be `ready` for HTTP 200. Any unavailable dependency returns 503 with `status: not_ready`.
Sync is unavailable before the first successful reconciliation or after 15 minutes without one.
Details and troubleshooting are in [observability](observability.md#health).
