# Runner operations

Issue: [#43](https://github.com/Rajveerx11/pr-reliability-platform/issues/43).
Decisions: [DEC-004](../plan/v1.md#dec-004--use-temporal-for-durable-workflows),
[DEC-005](../plan/v1.md#dec-005--use-postgresql-with-stable-ownership-fields),
[DEC-015](../plan/v1.md#dec-015--require-production-evidence-before-rollout).

Open `/operations` as an administrator. The overview and metric APIs require the existing
GitHub session, live repository access and owner authorization. Drain requests also require
same-origin CSRF proof. The static page has no private data. Reviewer-only users cannot view
runner infrastructure. Work counts follow the administrator's live repository access; runner
capacity is shared within the owner. No browser heartbeat or public scrape endpoint exists.

## What the numbers mean

- Queued: durable `runs.state = queued`, without accepted Temporal dispatch.
- Assigned: queued runs with a real accepted `run.command_dispatched` receipt. This means
  assigned to Temporal, **not** assigned to a particular host or reserved activity slot.
- Running: durable selecting-context, analyzing or verifying stages. A retry or dependency
  outage can leave a stage running even when no worker slot is busy.
- Awaiting approval: separate durable human wait, not worker utilization.
- Cancelled: terminal cancelled runs. Completed: published, rejected or failed runs.
- Queue depth: undispatched queued runs plus actual waiting Temporal activities. The existing
  workflow runner monitor reads owned active PR workflows with DescribeWorkflowExecution every
  heartbeat. This includes analyze after completed context selection and server retry backoff;
  started activities and cancellation requests are not waiting slots. Current wait is the oldest
  undispatched run age or pending activity scheduled age. A nonempty known depth is a lower bound
  when any PR observations are unknown; `queue_observation_unknown` reports that count. No fresh
  observations and no known backlog means Unknown, never a false zero. Observations expire after
  45 seconds. An unavailable queue probe emits a fixed private alert.
- p50/p95 wait: run creation to first observed activity start, including the initial Check Run
  activity. Starts are recorded once across retries. Old missing starts remain Unknown; sample
  and missing-history counts are returned. This is not Temporal schedule-to-start latency.
- Pass rate: successful recorded verification receipts divided by all passed/failed verification
  receipts. Approval rejection, publication and cancellation are not test results.
- Active workers/capacity: fresh, non-draining review activity workers and their configured
  activity slots. Utilization: their observed busy slots divided by their capacity; no capacity
  means Unknown, not zero. Values are sampled every 10 seconds.
- Workflow-task workers have heartbeat/version/workload and online/draining/offline status.
  Their task occupancy and capacity are Unknown, not inferred from waiting workflows.

Current pending activity retry backlog is observed, but historical attempts and worker
assignment history are not read here. #39 still owns exact historical attempt/retry facts. One owner uses one deployment
queue. Undispatched/historical runs have no queue observation, so they belong to that configured
queue. Do not use this API for multi-queue routing. Runner lists are capped at 1,000 stable
operator identities. Use stable deployment slot names, not names per run or restart.

`GET /api/operations/metrics` emits fixed Prometheus gauge names with only the configured queue
label. Unknown values are omitted. Never add runner IDs, versions, PRs, run IDs, file paths,
source or exception text as metric labels. It is a private browser-session API, not a new
machine-token authentication scheme; deployment scraping must use an approved identity.

## Installed deployment monitoring

Local and VM Compose now give both mandatory worker monitors database/owner/build/runner
configuration, migration readiness dependencies, and 90-second shutdown grace. The VM also
waits for healthy PostgreSQL. Set unique stable runner slot IDs and the actual `RUNNER_VERSION`
build identifier; `RUNNER_CAPACITY` sets the review worker's Temporal activity concurrency.
Apply reserved migration `0010` without renaming or touching #42's `0009`.

Both Compose manifests install `operations-monitor`, which checks every 30 seconds, and
Prometheus scrapes its fixed, unlabeled success-time gauge. `OperationsMonitorUnavailable`
fires on missing/down monitoring or a check/delivery success older than 120 seconds (for 2m).
The monitor tries a fixed `alert_check_unavailable` private notification on DB failure.
Prometheus failure rules are installed; routing those independent Prometheus alerts to an
operator's monitoring receiver remains an external deployment integration, not a claimed live
notification. The six operational alerts themselves are sent directly by the periodic monitor.

Before starting, supply these **external, operator-approved** files/paths (examples are not live
approval):

- `OPERATIONS_ALERT_CONFIG_FILE`: copy `infra/observability/operations-alerts.example.json`
  outside the checkout and replace its rejected placeholder with the approved RFC1918/ULA
  literal-IP HTTPS URL (port 443), using a trusted server certificate.
- `OPERATIONS_ALERT_TOKEN_FILE`: readable private receiver token file, mounted read-only only
  into the monitor. No token goes to the workflow worker or Prometheus.
- `OPERATIONS_DISK_PATH`: the actual host data/backup filesystem directory, mounted read-only
  as `/probes/disk`, not the container overlay filesystem.
- `OPERATIONS_BACKUP_RECEIPT_DIRECTORY`: the receipt directory, mounted read-only at
  `/probes/backup`. The shipped systemd backup job writes `/var/lib/pr-reliability/operations/backup.json`
  atomically with fixed boolean/time metadata, recording failure before each attempt and success
  only after backup completion. Enable the existing backup timer as part of deployment.
- `TLS_CERTIFICATE_FILE`: the actual served certificate mounted read-only at `/probes/tls.pem`.

The installed example config points at those container probe paths. The token/config must be
readable by the unprivileged monitor (VM deployment secret group); do not make secrets public.
DNS/public/loopback/link-local receivers, URL credentials, query strings and redirects are
rejected. No environment proxy is used; TLS verification is never disabled.
`pr-reliability-operation-alerts --config /private/config.json --once` remains available for a
single local check. Repeated fixed service/code pairs must be deduplicated by the receiver.

Alerts cover missing workflow/review workers, oldest queue wait over threshold, three or more
failed completions in an hour at a failure ratio of at least 50%, disk free below 10%, failed/stale
backups and expiring TLS. Missing/malformed probes and unavailable Temporal observations are
explicit unavailable alerts, never healthy results. Payloads contain fixed schema/service/code/
severity only, never source, repository/runner/owner names, paths, IDs or exception text.

The wiring is installed, not a claim of live private receiver delivery, Linux shutdown behavior,
real host probes, backup restoration or alert routing acceptance. Those require an operator drill.

## Drain and recovery

Click Drain or send SIGTERM. The durable drain flag is read on the next heartbeat. The worker
reports draining, stops Temporal polling and lets in-flight activities finish for up to the
60-second SDK grace. Cleanup can extend shutdown. Interrupted activity attempts are still
Temporal's durable work; this code never cancels workflows or deletes commands/run state.
A clean shutdown retires the current session and acknowledges the drain. Restart resumes
with a new random session and resets slot observations. A pending drain survives a crash until
it is acknowledged. Restart checks a pending drain before polling, retires that session and
exits without accepting new work. A replacement process fences older heartbeat/start writers
and asks the older process to drain; do not intentionally overlap processes sharing a runner ID.

Database loss before an activity start prevents a new provider side effect and lets Temporal
retry. Heartbeat dependency failures log only a fixed code. Temporal health failure reports
capacity offline; database loss eventually makes the heartbeat stale. A heartbeat older than
45 seconds is offline, even if its last stored state was online or busy. No dependency failure
removes durable work or guesses a terminal result.

## Checked acceptance facts

| Issue #43 criterion | Repository evidence | Boundary |
| --- | --- | --- |
| Heartbeat, version, workload and state | Migration 0010, fenced registrations, stale/offline and busy/draining tests | Workflow task occupancy is Unknown; heartbeat freshness is sampled |
| Queued, assigned, running, cancelled, completed | Owner/repository-scoped durable runs and accepted dispatch receipts | Assigned means Temporal dispatch, not host reservation |
| Queue, wait, capacity, utilization, pass rate | SQL counts/percentiles, activity interception, bounded private metrics and browser rendering | Current activity queues include later stages/retry backoff; historical percentiles remain first starts |
| All required alerts | Fixed missing-worker, stuck-queue, repeated-failure, disk, backup and TLS checks with regression tests | Compose installs a 30-second monitor and read-only host probes; receiver approval stays external |
| Approved private delivery without private content | Literal private HTTPS receiver validation, no redirects/proxies, fixed allowlisted payload, auth-header tests | Actual approval and receiver delivery are not verified |
| Drain without durable work loss | Real Temporal drain/replacement test; pending crash-drain is checked before polling | Real VM shutdown drill remains required |
| Bounded metrics | Only a bounded configured queue label; work counts use live repository authorization | One queue per owner; no multi-queue routing or public scrape |
| Restart and dependency-loss recovery | Real Temporal retry across worker replacement with real PostgreSQL observations | Outage is injected into this test's connection factory, not a real VM dependency |

## Evidence and remaining acceptance

Tests beside API and worker code cover auth, CSRF, owner/repository isolation, lifecycle counts,
Unknown history, stale heartbeats, session fencing, drain requests, dependency failure, fixed
private delivery metadata and host-probe failures. One real local Temporal test drains an
active activity and proves queued work runs only after the replacement worker starts. Another
uses real PostgreSQL observations and a test-only failed connection factory to prove a blocked
activity retries across worker replacement without an early provider side effect or lost work.
It does not stop a real database service or use real provider credentials. Browser tests in
`apps/web/tests/operations.browser.js` exercise desktop/mobile render, keyboard drain, visible
heartbeat, failed drain, unavailable, forbidden, signed-out and delayed-response clearing states
with isolated HTTP fixtures. The shared dashboard links to the operations page.

Recovery validation on 2026-10-08:

- `uv run ruff format --check .`: 198 files already formatted.
- `uv run ruff check .`, `git diff 9166409 --check`, and Node syntax checks for both new
  operations JavaScript files passed. The recovered unused test variable was fixed.
- Focused API/auth/migration/runner/alert tests: **45 passed**, one existing FastAPI/httpx
  deprecation warning, with PostgreSQL 18 on task-owned port 5443 and UTF8 database
  `issue43_utf8`. Initial attempts used the wrong test role and errored; the corrected role
  and database were verified before the passing run.
- Unfiltered `uv run pytest -q -ra`: **546 passed, 1 failed, 17 skipped**, one warning.
  Failure: `workers/tests/test_production_operations.py::test_operations_persist_only_safe_receipts_and_replay_analysis`
  cannot unlink a read-only Windows Git pack in existing provider cleanup. The same test
  fails on the unchanged 9166409 archive (1 failed). That provider/artifact code was not
  changed. No `-k`, `--ignore`, or arbitrary exclusion was used for this broad run.
- Existing skips: 8 dedicated Docker sandbox tests, 7 Linux descendant-supervision tests,
  and 2 POSIX socket-ownership tests. Docker has no running Linux daemon here. These are
  unverified boundaries, not a clean integration result.
- Playwright skill runner: **28 assertions passed** at 1280 and 390 pixels against local
  source assets and isolated HTTP fixtures. Screenshots were inspected; both fit their
  viewport. The temporary asset server used UTF8, matching production asset routes.
- `uv build` passed for wheel and sdist. The wheel includes operations HTML/JavaScript,
  migration 0010 and the dashboard navigation link.
- LSP validation was not available in this worker's tool allowlist. No substitute LSP
  script was created. Independent review and CodeRabbit are owned by the parent.

Full acceptance is **not complete** while the broad check fails and these boundaries remain.
Still required: independent review/CodeRabbit, Linux CI, real VM dependency/restart drill,
approved receiver delivery, actual backup receipt/restore and TLS/disk probe evidence, and
independent failure-notification routing. Do not treat repository or fixture evidence as live
production acceptance.

### Focused review corrections (2026-10-08)

The three independent blockers were corrected after recovery: both supported manifests supply
mandatory runner settings and readiness/grace; periodic monitoring, probes, backup receipts and
independent Prometheus failure detection are installed; queue observations now come from real
Temporal pending activities via the existing workflow heartbeat loop. Focused regressions cover
actual context completion followed by analyze waiting behind verification, plus retry backoff,
unknown/stale observations and rendered deployment configuration. This is not a fresh independent
review or production acceptance. The parent must run full CI and obtain re-review.
