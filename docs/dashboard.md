# Review operations dashboard

Open `/dashboard` on the API service. The current dashboard is private and read-only. Planned
production pages are listed separately below.

## Current access

Select **Sign in with GitHub** on the private HTTPS origin. The dashboard shows your login and
reviewer/admin role. Data is limited to repositories you can currently access through the
configured installation. Sessions use Secure, HttpOnly cookies; no credential is entered into
or stored by the page. **Sign out** deletes the local session, including during a GitHub outage.
See [authentication](authentication.md) for operator configuration and revocation.

## Current dashboard

- Owner-scoped run totals and states.
- Findings awaiting approval.
- p50 and p95 duration for terminal runs.
- Recent runs with repository and status filters.
- Run timeline, trace ID, findings, and safe evidence.
- Overall API readiness, with PostgreSQL and Temporal detail labels. Sync freshness participates
  in overall readiness but has no separate dashboard card yet.
- Honest usage coverage and known cost.

Retries, usage, and cost show `Unknown` when no persisted fact exists. The API never estimates
missing values. Safe timeline summaries exclude source, prompts, raw model output, and secrets.

## Planned production pages

### Overview

Review volume, success rate, approval backlog, p50/p95 duration, usage coverage, known cost, queue
wait, failure rate, and recent incidents.

### Repositories

Issue #37 supplies the authenticated inventory and policy APIs: installation/access state,
enabled state, default branch, sync/webhook/review timestamps, branch rules, and budgets.
The repository page, open PR counts, and complete history UI still require #38. Until then use
[repository policy](repository-policy.md#private-api) or [API reference](api.md).

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

- Installation inventory is synchronized, but a repository/history UI is not yet implemented (#38).
- Retry, usage, and cost facts are incomplete until issue #39.
- Test summaries and bounded logs require issue #42.
- Check Run detail links open the exact run after private dashboard authentication. Dashboard list
  columns for Check Run state remain part of repository/history work in issue #38.

## Operations

Use the **Approval inbox** for decisions. The dashboard cannot publish comments. Readiness comes
from `/health/ready`; trace IDs map to [observability](observability.md).

Expose the dashboard through the same private HTTPS origin as the API. Do not expose PostgreSQL,
Temporal, Prometheus, or the collector to the public internet.
