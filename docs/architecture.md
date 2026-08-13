# Architecture

## Goal

Review a GitHub pull request with one agent, verify the result, and require human approval
before any output is published.

## Main components

```mermaid
flowchart TB
  G[GitHub App] --> A[FastAPI control plane]
  A --> P[(PostgreSQL)]
  A --> T[Temporal workflow]
  T --> C[Context selector]
  T --> M[Review agent]
  T --> S[Sandbox runner]
  S --> W[Proof of Work adapter]
  T --> P
  P --> U[Approval inbox]
  U --> H[Human reviewer]
  H --> T
  T --> G
  T --> O[OpenTelemetry]
```

## Ownership

- FastAPI owns authentication, webhook intake, API validation, and product records.
- Temporal owns run order, retries, cancellation, timeouts, and approval waits.
- Context selector owns file selection under a fixed token budget.
- Review agent owns structured proposed findings. It cannot publish them.
- Sandbox runner owns untrusted test execution.
- Proof of Work adapter owns the stable verification boundary.
- Web application owns review and approval screens.
- PostgreSQL owns product state and append-only audit events.

## Run boundary

One run is identified by repository, pull request number, and head SHA. A new head SHA creates
a new run and cancels the older active run. Findings never move between commits.

GitHub webhook intake verifies the HMAC SHA-256 signature over raw bytes before decoding JSON.
Only pull request opened, reopened, synchronize, and closed actions are accepted. Delivery IDs are
inserted in the same database transaction as repository, pull request, run, and command-event
records. A repeated owner and delivery ID returns success without creating another command.
The configured GitHub App installation is bound to one owner; validly signed events from other
installations are rejected before any database write. Command events store the complete versioned
`StartRunCommand`, never raw webhook payloads.

## Message boundary

Messages use strict versioned JSON. A contract includes:

- `schema_version`
- `public_id`
- `owner_id`
- `run_id`
- `head_sha`
- event-specific payload

Unknown fields are rejected in version one. New optional fields require a minor contract
version. Breaking changes require a new major version and explicit migration.

Version-one contracts are defined in `packages/contracts/`. All contracts are immutable and
reject unknown fields. Run-bound messages carry `schema_version`, `public_id`, `owner_id`,
`run_id`, and `head_sha`. Webhook envelopes carry delivery, installation, repository, and pull
request identity before a run exists.

The run state machine is:

```text
queued -> selecting_context -> analyzing -> verifying -> awaiting_approval
                                                             |        |
                                                             v        v
                                                         published  rejected
```

Active states may also end as `failed` or `cancelled`. Terminal states cannot restart.

## Persistence boundary

PostgreSQL stores summaries and safe evidence references. It does not store repository source,
secrets, raw prompts, full agent output, or sandbox contents. Temporal history stores safe
workflow arguments only.

The first migration creates repositories, pull requests, runs, findings, approvals, external
actions, and append-only run events. Every product row has a public ULID and an `owner_id` ULID.
Relationships use bigint identity keys plus composite foreign keys that require child and parent
ownership to match. A pull request can have only one run for each head SHA. Finding keys,
approval decisions, external action targets, and event keys are unique within their retry
boundary. PostgreSQL rejects updates and deletes against the audit-event table.

Migrations run in filename order under a PostgreSQL advisory lock. Applied checksums are stored
in `schema_migrations`; changing an applied migration stops startup instead of silently changing
database history.

## Write boundary

Every external write follows this order:

1. Build a proposed action.
2. Verify evidence.
3. Store proposed action.
4. Wait for human decision.
5. Publish with an idempotency key.
6. Store remote result.

No worker may bypass this path.
