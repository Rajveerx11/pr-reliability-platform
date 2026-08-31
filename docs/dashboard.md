# Review operations dashboard

The dashboard gives one private, read-only view of review runs, approval pressure, latency,
dependency health, and persisted evidence. Open `/dashboard` on the API service.

## Access

Enter the same `APPROVAL_REVIEWER_TOKEN` used by the approval inbox. The token stays in memory for
the current browser tab. The dashboard does not write it to cookies, local storage, or session
storage. The browser sends it as a bearer token only to same-origin dashboard APIs.

Serve the dashboard only behind HTTPS. Keep the API private or behind an authenticated network
boundary. Rotate the reviewer token if it may have been exposed.

## What the dashboard shows

- owner-scoped run totals and current states;
- findings awaiting human approval;
- p50 and p95 duration for terminal runs;
- activity retry visibility, shown as `Unknown` until retry facts are persisted per run;
- recent runs with exact repository and status filters plus bounded pagination;
- a run detail timeline, persisted stage progress, trace ID, findings, and evidence;
- database and workflow readiness; and
- honest usage coverage and known cost.

Retries, usage, and cost remain `Unknown` until the current persistence model records those facts
per run. The dashboard does not infer missing values or treat budgets as spend.

The detail timeline exposes a fixed safe summary for known event types. It does not return raw
event payloads, prompts, source files, provider output, or secrets.

## Operations

Use the **Approval inbox** link when a finding needs a decision. The dashboard itself is read-only
and cannot publish comments or mutate a run.

Readiness comes from `/health/ready`. A failed dependency appears as **Needs attention**. Trace IDs
can be matched with the collector described in `observability.md`.

For a private VM deployment, expose the dashboard through the same Caddy HTTPS origin as the API.
Do not expose PostgreSQL, Temporal, Prometheus, or the collector directly to the public internet.
