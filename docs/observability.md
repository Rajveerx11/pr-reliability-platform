# Observability

## Trace one run

Webhook intake starts a W3C trace and saves only its `traceparent` in the versioned start-run
command. The dispatcher restores that parent before Temporal signal-with-start. Temporal then
propagates the same trace through workflow and activity workers. Every superseding or reopened run
generation carries its own saved parent through continue-as-new, so its activities remain searchable
from that generation's webhook trace ID.

Search collector trace output by `pr.run.id` or by the `X-Trace-Id` returned from the webhook API.
Activity spans identify model work, tools, the Temporal attempt, the head SHA, and reported usage.
Approval waits are `approval.wait` span events with measured duration. Baggage, prompts, source,
model output, and
secrets are never persisted or attached.

## Run metrics

The local collector exposes Prometheus metrics on port `8889`:

- `pr.run.duration` is a histogram in seconds. Calculate p50 and p95 from its histogram buckets.
- `pr.activity.duration` separates model, tool, publish, and persistence latency.
- `pr.activity.retries` counts attempts after the first.
- `pr.run.usage` has `pr.usage.status=complete|partial|unknown` and
  `pr.usage.known=true` only when both token counts are reported. Partial and unknown provider
  usage remains visible and is never estimated or recorded as zero.

Prometheus p50 and p95 examples:

```promql
histogram_quantile(0.50, sum by (le) (rate(pr_run_duration_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le) (rate(pr_run_duration_seconds_bucket[5m])))
```

Provider adapters return `ModelUsage` only when the provider reports it. Input tokens, output
tokens, and exact micro-dollar cost become span attributes. `pr.usage.tokens_known` and
`pr.cost.known` make partially missing values explicit.

## Product metrics and dashboard state

OpenTelemetry metrics are runtime signals. They do not replace durable product facts. Today the
dashboard shows `Unknown` when retry, usage, or cost data is not stored per run.

Issue #39 adds owner-scoped volume, outcome, retry, latency, usage, and cost facts. Issue #43 adds
queue depth, oldest queued age, runner capacity, cancellations, reruns, and alert delivery. Metric
labels must stay bounded; never use repository names, PR numbers, run IDs, or source paths as
Prometheus labels.

## Health

- `GET /health/live` proves the API process can answer.
- Production `GET /health/ready` checks PostgreSQL, Temporal workflow-service health, and the
  configured owner/installation's last successful sync. Its `dependencies` object contains
  `database`, `workflow`, and `repository_sync`. It returns `503` unless all three are ready.
  Missing sync or a last success older than 15 minutes reports `repository_sync: unavailable`.
  Fresh confirmed suspension/deletion is operationally healthy while admission remains blocked. Each check is bounded by
  `HEALTH_CHECK_TIMEOUT_SECONDS`, which defaults to two seconds. PostgreSQL uses an async
  connection, server statement deadline, and guaranteed connection close on timeout, so repeated
  failed probes do not leave blocked worker threads or open clients. Concurrent readiness requests
  share one active dependency probe, preventing probe storms from multiplying database connections
  or Temporal RPCs.
- The collector health endpoint is exposed on port `13133`.

The deployment health command checks required container processes and `/health/ready`, so a
running sync process with persistent errors cannot remain healthy after inventory expires.
The existing dashboard renders the overall readiness status and separate database/workflow
labels; it does not yet have a dedicated sync detail card. Use the readiness JSON for that detail.

For `repository_sync: unavailable`, check migration success, configured owner/installation IDs,
private-key file access, App credentials, and GitHub network access. Run the sync worker's `--once`
command in the configured environment to obtain a safe success/failure exit status. The daemon
retries every 60 seconds and logs a generic failure message without credentials. The authenticated
inventory API exposes timestamps; append-only repository events record successful changes.
Never paste key contents or provider response bodies into incident reports. Dedicated sync failure
metrics and alert delivery are not implemented; broader operations remain #43.

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to another OTLP/HTTP collector to use a hosted trace and metrics
backend. Compose uses `http://otel-collector:4318`; processes run directly on the host should use
`http://localhost:4318`. Leaving it unset keeps instrumentation active without exporting telemetry.
The baseline local Compose file publishes metrics and health only; a host exporter needs a
separate collector or a reviewed loopback-only OTLP port mapping. See [development](development.md).
