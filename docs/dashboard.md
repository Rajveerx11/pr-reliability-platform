# Review operations dashboard

Open `/dashboard` on the API service. The current dashboard is private and read-only. Planned
production pages are listed separately below.

## Current access

Enter the same `APPROVAL_REVIEWER_TOKEN` used by the approval inbox. The token stays in memory
for the browser tab and is sent only to same-origin APIs. It is not stored in cookies, local
storage, or session storage.

This shared token is temporary. Production access must use GitHub login and owner-scoped sessions
from [issue #44](https://github.com/Rajveerx11/pr-reliability-platform/issues/44).

## Current dashboard

- Owner-scoped run totals and states.
- Findings awaiting approval.
- p50 and p95 duration for terminal runs.
- Recent runs with repository and status filters.
- Run timeline, trace ID, findings, and safe evidence.
- PostgreSQL and Temporal readiness.
- Honest usage coverage and known cost.

Retries, usage, and cost show `Unknown` when no persisted fact exists. The API never estimates
missing values. Safe timeline summaries exclude source, prompts, raw model output, and secrets.

## Planned production pages

### Overview

Review volume, success rate, approval backlog, p50/p95 duration, usage coverage, known cost, queue
wait, failure rate, and recent incidents.

### Repositories

All repositories visible to the GitHub App installation, active or paused state, default branch,
last sync, policy, open PR count, last review, and health. This requires issue #37.

### Pull request history

Open and closed pull requests, reviewed commits, run count, findings, approvals, Check Run result,
publish result, duration, usage, and cost. This requires issue #38.

### Run detail

Stage timeline, model and tool attempts, repository checks, bounded logs, test summary, evidence,
approval, GitHub writes, retries, and safe rerun or cancel controls.

### System health

API, PostgreSQL, Temporal, dispatcher, workers, queue depth, oldest queued run, runner capacity,
error rate, and alert state. This requires issue #43.

## Data gaps to close

- Repository inventory is learned from pull request webhooks, not installation sync.
- Retry, usage, and cost facts are incomplete until issue #39.
- Test summaries and bounded logs require issue #42.
- GitHub Check Run state requires issue #40.

## Operations

Use the **Approval inbox** for decisions. The dashboard cannot publish comments. Readiness comes
from `/health/ready`; trace IDs map to [observability](observability.md).

Expose the dashboard through the same private HTTPS origin as the API. Do not expose PostgreSQL,
Temporal, Prometheus, or the collector to the public internet.
