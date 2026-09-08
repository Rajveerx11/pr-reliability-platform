# Review checks and CI boundary

## Decision

The product may run small CI-style checks when they improve review evidence, speed, or GitHub
visibility. It will not become a general CI/CD system or a GitHub Actions replacement.

## Implemented check boundary

The exact reviewed head may define up to 16 checks in `.pr-reliability.json`. The worker accepts
only schema version `1`, exact documented fields, safe check names, immutable images, argument-vector
commands, bounded path filters, and resource values within both operator and platform maxima.
Unknown, duplicate, missing, oversized, or malformed configuration fails verification without a
retry. Missing configuration also fails closed.

Every selected check runs sequentially through the existing disposable rootless Linux Docker
boundary. Network is always `none`; the container receives only `HOME` and `TMPDIR`, never GitHub,
provider, database, deployment, or runner credentials. There is no host-command fallback.

Example repository configuration:

```json
{
  "schema_version": "1",
  "checks": [
    {
      "name": "python-tests",
      "image": "registry.internal/checks@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "command": ["python", "-m", "pytest", "-q"],
      "paths": ["**/*.py", "pyproject.toml"],
      "timeout_seconds": 300,
      "resources": {
        "cpu_count": 1,
        "memory_bytes": 536870912,
        "pids": 128,
        "workspace_bytes": 268435456,
        "workspace_entries": 50000,
        "temp_bytes": 67108864,
        "output_bytes": 1048576
      }
    }
  ]
}
```

`REVIEW_CHECK_ALLOWLIST_JSON` defines trusted operator policy. Each entry has `name`, `image`,
`command`, and optional `maximum_resources`. Repository image and command must match exactly.
Repository resource values may reduce limits but cannot raise them. Allowlist and repository
configuration changes require worker restart and a new reviewed commit, respectively. Selected
checks share a 15-minute aggregate command-time budget. The Temporal verification activity allows
that budget, bounded container control and checkout work, one bounded Proof run, and cleanup under
a two-hour outer hard stop. Persisted verification receipts prevent retry from rerunning checks or
changing nondeterministic evidence such as duration.

Paths use repository-relative `/` separators. A check runs when any changed path matches one of
its patterns. A change to `.pr-reliability.json` runs every declared check. Evidence records
`configuration_changed`, `path_match`, or `no_matching_paths`, plus check status, duration, exit
status, timeout, and output-limit facts. Raw output remains ephemeral until issue #42.
Sandbox unavailable, runtime, and cleanup failures record distinct fixed codes without exception
text before verification stops.

## Approved scope still to deliver

- One GitHub Check Run for each reviewed head SHA.
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
