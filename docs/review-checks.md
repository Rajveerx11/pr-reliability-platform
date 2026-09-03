# Review checks and CI boundary

## Decision

The product may run small CI-style checks when they improve review evidence, speed, or GitHub
visibility. It will not become a general CI/CD system or a GitHub Actions replacement.

## Included

- One GitHub Check Run for each reviewed head SHA.
- Repository-defined lint, test, type-check, or build commands from an allow-listed config.
- Path filters so unchanged areas do not run unnecessary checks.
- Disposable sandbox execution with no network by default.
- Fixed image, CPU, memory, process, filesystem, output, and time limits.
- Bounded logs, parsed test summaries, cancellation, and safe reruns.
- Queue depth, wait time, runner capacity, failure rate, and duration metrics.

## Excluded

- Application or infrastructure deployment.
- GitHub Actions syntax compatibility.
- Marketplace action execution.
- Arbitrary host commands or privileged containers.
- Shared writable workspaces between repositories.
- Secrets in forked pull request checks.
- General release, packaging, environment, or test-matrix orchestration.

## GitHub behavior

[Issue #40](https://github.com/Rajveerx11/pr-reliability-platform/issues/40) adds one Check Run
linked to the review run. It should move from queued to in progress to a terminal conclusion,
include a short safe summary, and link to the dashboard. Repeated webhooks must update the same
check for the same repository, pull request, and head SHA.

## Repository configuration

[Issue #41](https://github.com/Rajveerx11/pr-reliability-platform/issues/41) adds a small,
versioned repository config. It may choose from approved commands and path filters. It cannot
choose the container image, enable network, mount host paths, request secrets, or raise limits.
Invalid or missing configuration fails safely and appears in the dashboard.

## Execution rules

- Treat configuration and pull request source as untrusted input.
- Run only inside the existing disposable Linux sandbox.
- Use a clean snapshot of the reviewed head SHA.
- Never fall back to host execution.
- Cancel obsolete work when a newer head arrives.
- Never expose GitHub, provider, database, or runner credentials to a check.
- Do not provide secrets to forked pull requests.

## Evidence and operations

[Issue #42](https://github.com/Rajveerx11/pr-reliability-platform/issues/42) stores bounded output,
test totals, duration, exit status, truncation, and artifact expiry. Raw source and secrets remain
forbidden. [Issue #43](https://github.com/Rajveerx11/pr-reliability-platform/issues/43) adds queue
and runner visibility, alerts, cancellation, and rerun controls.
